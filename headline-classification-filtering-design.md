# Headline Classification and Category Filtering Design

## Implementation Status

Implemented and deployed in commit `ef2bb21` (`Add headline category filtering pipeline`).

The current implementation includes:

- per-user category-interest selection in Settings;
- headline-only classification before full-text retrieval;
- explicit `Uncertain` handling;
- body-based categorization as the authoritative result;
- early and late filtering with a terminal `filtered` state;
- migration `0013_headline_category_filter`;
- Activity-page visibility for recent `classify` LLM calls;
- 47 passing targeted backend tests, passing Ruff checks, and a successful Docker build.

The clean test environment is active on Snorlax. It currently has one admin account and no feeds, articles, or stories until a test feed is added.

## Purpose

Reduce unnecessary Ollama processing by classifying RSS headlines immediately after ingestion, before fetching article bodies and running the existing full summarization pipeline.

The headline classification is only a preliminary decision. The category assigned after reading the full article body remains authoritative.

## Proposed Pipeline

```text
RSS ingestion
  -> preliminary headline classification
      -> unwanted category: mark filtered; stop
      -> in-scope category: continue
      -> Uncertain: continue
  -> fetch article body
  -> existing body summarization and categorization
      -> unwanted final category: mark filtered; stop
      -> wanted final category: embed and cluster
```

The existing downstream processing for accepted articles remains:

```text
fulltext -> summarized -> embedded -> clustered
```

## Stage 1: Headline Classification

After RSS articles are inserted and the insert transaction is committed, send a small payload to Ollama containing at least:

- article headline;
- feed title, if available;
- the current category taxonomy;
- the configured category-interest scope.

The classifier should return structured JSON. The conceptual result is:

```json
{"category": "Business"}
```

or, when the headline does not contain enough information:

```json
{"category": "Uncertain"}
```

The classifier should be instructed to return `Uncertain` whenever the headline alone is ambiguous. Numeric confidence scores should not be treated as authoritative because local LLM confidence values are not reliably calibrated.

### Preliminary outcomes

| Headline result | Action |
|---|---|
| Category is outside the user’s interests | Mark the article `filtered`; do not fetch the body or summarize it |
| Category is in scope | Continue to full-text retrieval |
| `Uncertain` | Continue to full-text retrieval |
| Classification request fails | Treat as `Uncertain` or retry according to the normal LLM failure policy; do not silently discard the article |

The initial classification should not overwrite the authoritative article category. It should be stored separately, for example as `headline_category` or `preliminary_category`.

## Stage 2: Existing Body Summarization

Articles that pass the headline gate or are marked `Uncertain` continue through the current pipeline:

1. Fetch full text, using the existing direct, Archive.is, and RSS-excerpt fallback chain.
2. Send the article body to the existing summarization prompt.
3. Ask Ollama for both a summary and one category from the taxonomy.
4. Store that category as the article’s authoritative final category.

The existing body-based result takes precedence over the headline result.

If the final body category is outside the user’s selected categories, the article should be marked `filtered` immediately after summarization. It must not be embedded, clustered, or become a visible story.

If the final category is in scope, processing continues with the existing embedding and clustering stages.

```text
body category in scope
  -> embed article
  -> cluster article into a story

body category out of scope
  -> mark filtered
  -> stop
```

## Processing States

The current article state machine is:

```text
fetched -> fulltext -> summarized -> embedded -> clustered
```

The proposed design needs an additional terminal state:

```text
filtered
```

Suggested behavior:

```text
fetched -> headline classified
  -> filtered
  -> fulltext -> summarized
       -> filtered
       -> embedded -> clustered
```

The backlog sweep currently requeues articles in `fulltext`. Filtered articles must not remain in that state, or they will be retried and sent to Ollama again.

The activity pipeline and any progress indicators should recognize `filtered` as a completed terminal outcome rather than an article stuck in processing.

## Persistence and Reconsideration

Filtered articles should normally be retained in the database rather than deleted. Recommended stored information:

- article headline and RSS metadata;
- preliminary/headline category;
- final body category, when body classification occurred;
- the reason for filtering, such as `headline_out_of_scope` or `body_out_of_scope`;
- processing state `filtered`.

Retaining the article allows a future preference change to reconsider it. A reprocessing action would need to reset the article to an appropriate earlier state and run the body fetch/summarization path again if necessary.

## User Preferences

The current `story_filter` preference means `all`, `unread`, or `updated`; it is not a category-interest setting. A separate preference is required for interested categories.

The preference should be validated against the current `Category` taxonomy. Category rename and deletion behavior will need to define how existing user selections are updated.

For a single-user installation, the preference can act as the global processing gate. For multiple users, articles and stories are currently shared globally, so an article should not be globally filtered merely because one user does not want its category. A shared article should generally be processed if at least one user is interested in its category, unless processing is redesigned to be user-specific.

## Ollama Call and Performance Considerations

This design adds one lightweight classification call for every newly ingested article, but can avoid the more expensive operations for clearly unwanted articles:

- full-text retrieval;
- full article summarization;
- article embedding;
- clustering and any related LLM calls.

The initial implementation can classify one headline at a time through the existing JSON LLM client. If this creates too much latency, headlines could later be batched into a single request, with one structured result per article.

When no user has configured category interests, the gate is bypassed and all articles follow the existing processing behavior. If multiple users exist, an empty preference means that user wants all categories; therefore filtering is only active when every user has a non-empty preference, and the effective scope is the union of those preferences.

The classification call must not run while SQLite writer changes are left uncommitted. The ingestion code intentionally avoids holding the writer lock across slow network and LLM operations. A safe sequence is:

```text
insert RSS articles
  -> commit
  -> classify headlines
  -> update classification state
  -> fetch bodies for in-scope/uncertain articles
```

## Accuracy Considerations

Headline-only classification will work best for explicit headlines and less reliably for vague or context-dependent headlines. Including the feed title improves context at little token cost. The `Uncertain` fallback is important because an ambiguous headline should receive body-based classification rather than being prematurely discarded.

The body-based category remains the source of truth even when the preliminary classifier is confident. This allows the system to recover from headline misclassification for every article that reaches the body stage.

## Scope of This Document

This document records the proposed behavior and implementation constraints. It does not authorize or include:

- database migrations;
- changes to the processing worker;
- changes to the frontend;
- changes to Ollama configuration;
- changes to the running Docker container;
- deletion of filtered articles.

The items above describe the original implementation scope; the implementation status at the top of this document records what has now been completed.
