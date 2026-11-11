# Web IQ Browse: vendor security questionnaire

Pre-fill a vendor security questionnaire from the vendor's public pages. A procurement or security team lists the vendor's trust, privacy, and security page URLs and a standard set of questions. For each question and page, the sample calls [Browse](https://learn.microsoft.com/en-us/web-iq/reference/browse) with the question as the query, and shows the best one or two sources for each answer with a short quote, the link, and the crawl date.

Browse fits this job because the app already knows which pages to read. It returns passages: the parts of a page that match the question, not the whole page.

The sample calls the Browse REST API directly. It doesn't call a model: it prepares the passages as the input you'd send to your own model and shows where that call goes. Results stay in memory for the current run: no files, database, cache, or export.

## Prerequisites

- Python 3.11 or later
- An interactive terminal
- A [Web IQ API key](https://learn.microsoft.com/en-us/web-iq/get-started/quickstart) whose application has Browse access

Each question is sent to each page, so a run makes one Browse request per question and page: 40 requests for the example (8 questions, 5 pages). Requests incur [Web IQ charges](https://learn.microsoft.com/en-us/web-iq/resources/pricing). No SDK, model account, or model deployment is needed.

## Get started

Run these commands from this sample's directory.

### Windows (PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
$env:WEBIQ_API_KEY = "<your Web IQ API key>"
webiq-vendor-questionnaire
```

### macOS/Linux (Bash)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
export WEBIQ_API_KEY="<your Web IQ API key>"
webiq-vendor-questionnaire
```

The example uses Microsoft's own public trust and compliance pages in [vendor.json](vendor.json) and eight common questions in [questions.json](questions.json): data location, retention after the contract ends, certifications, encryption, subprocessors, breach notification, deletion requests, and use of customer data for AI training.

Keep keys out of source control, and use an approved secret store in production. Web IQ also supports Microsoft Entra ID; see [Authentication](https://learn.microsoft.com/en-us/web-iq/get-started/authentication).

## Use your own vendor and questions

Edit [vendor.json](vendor.json) with the vendor's name and up to 10 public page URLs (`https://` only), and [questions.json](questions.json) with up to 20 questions of up to 1,000 characters each. A run allows at most 100 requests.

Use pages you're allowed to use. Browse applies robots directives and site policies, and returns an error for pages it can't return.

## Options

| Command | Use it when |
| --- | --- |
| `webiq-vendor-questionnaire` | You want answers from pages already in the Web IQ index. |
| `webiq-vendor-questionnaire --live-crawl` | Some pages aren't in the index, and you want Browse to try to crawl them. |
| `webiq-vendor-questionnaire --vendor other-vendor.json --questions other-questions.json` | You keep several vendors or question sets. |
| `webiq-vendor-questionnaire --help` | You want to see the options without a key or a network call. |

## Request

For each question and page, the sample sends this request to `https://api.microsoft.ai/v3/browse` with the `x-apikey` header:

```json
{
  "url": "<a page from vendor.json>",
  "query": "<a question from questions.json>",
  "contentFormat": "passage",
  "maxLength": 3000,
  "liveCrawl": "none"
}
```

With `--live-crawl`, pages that return HTTP 404 (not in the index) are sent again with `"liveCrawl": "fallback"`. See [Passages and content formats](https://learn.microsoft.com/en-us/web-iq/concepts/content-formats) for how passages are selected.

## What Browse returns and what you show

**What Browse returns**: up to 3,000 characters of passages for each question and page. They're kept in memory as the input for your model (see [Add your own model](#add-your-own-model)) and never printed.

**What the terminal shows**: for each question, the best one or two sources, each with a quote cut in code from the part of the passage that contains the most question words (skipping the page title), the page title, the link, and the crawl date. Each page can be quoted for at most 200 characters in the whole run. A page cited in several answers shares those 200 characters, and once they're used, later answers link to the page without a quote. Each source line shows how many characters Browse returned and how many are shown, and the run ends with the totals, for example:

```text
[1] Where is customer data stored, and can the customer choose the region?
  Source 1: Example data location page
    "...Synthetic text: customer data is stored in the region the customer chooses when the account is created..."
    https://example.com/trust/data-location
    2,840 characters returned, ready for your model; 103 shown
    Crawled: 2026-11-04T08:00:00Z (2 days ago); last updated: not provided

[2] How long is customer data kept after the contract ends?
  Not found on these pages.

Browse returned 32 passages (61,200 characters) from 4 page(s), ready as model input in this run; 640 characters are shown here.
```

This example uses synthetic text and a reserved example domain, not saved API output.

The 200-character limit follows the Use and Display Requirements in the [Web IQ Terms of Use](https://www.microsoft.com/en-us/webiq/webiq-legal). A website owner can set a lower maximum, which the sample can't detect.

### How sources are picked

The sample ranks pages by how many of the question's distinctive words their passages contain, so it runs without a model. Words common to all questionnaire questions, such as "customer" and "data", don't count. A page needs at least two of the question's words (one if the question has only one). Words that appear on every page still count toward that, but they score less, so they help less in choosing between pages. A second source is shown only when it scores close to the first. "Not found on these pages" means no page matched well enough; it isn't a statement about the vendor. Your model, or a person, does the real reading.

## Live crawl

Live crawl is off by default (`"liveCrawl": "none"`), because large vendors' trust and privacy pages are usually in the index. With `--live-crawl`, the sample sends `"fallback"` only for pages that returned 404. Live crawl has a lower limit than indexed requests, so the sample sends live-crawl requests one at a time.

When a crawl is in progress, Browse returns HTTP 202 with a `retryAfter` time. The sample waits until then and sends the same request again, up to three times, and reports the page if the crawl doesn't finish during the run. `"fallback"` doesn't guarantee a crawl: publisher restrictions and availability apply.

## Reliability and errors

The client uses a 10-second connection timeout, 30-second read, write, and pool timeouts, at most three attempts per request, and four requests in parallel, spaced to stay below the default rate limit.

HTTP 429 and transient 408, 500, 502, 503, and 504 responses, plus network errors, are retried with exponential backoff and jitter. The sample respects both the `Retry-After` header and the `retryAfter` field in the JSON error, and waits for the longer one. Invalid guidance, or a required wait over 60 seconds, stops the run instead of retrying early. An exhausted daily quota isn't retried.

Errors that affect one page are reported for that page, and the run continues:

| Outcome | What the sample does |
| --- | --- |
| HTTP 404 (not in the index) | Reports the page and suggests `--live-crawl`. |
| HTTP 403 for the page (robots directives, site or content policy) | Reports that the page can't be returned. |
| HTTP 520, 523, or 524 (crawl or processing failed) | Reports the page; try again later. |
| Too many live crawls (429 `TooManyOnDemandCrawls`, or 430) | Waits and retries within the limits above, then reports the page. |

Errors that affect the whole run stop it:

| Outcome | What the sample does |
| --- | --- |
| Missing key or invalid `vendor.json` or `questions.json` | Stops before any network call, with a clear message. |
| HTTP 401 | Asks you to check the key and whether it expired. No retry. |
| HTTP 403 for the application | Asks you to confirm the application has Browse access. No retry. |
| HTTP 429 after retries, or an exhausted daily quota | Reports the quota or the required wait. |
| Invalid JSON or response shape | Reports an error instead of treating it as a passage. |

Error messages don't echo service response bodies, questions, keys, or retrieved content. Known error codes and well-formed trace IDs are shown so you can quote them to support. Redirects aren't followed. For all status and error codes, see [Error handling](https://learn.microsoft.com/en-us/web-iq/reference/error-handling). Exit codes: 0 for a completed run (including pages with no passages), 1 for errors, 2 for invalid options, 130 for a cancelled run.

## Add your own model

No model runs in this sample. To draft answers with your own model, add the call in `main()` in `cli.py`, after `build_results(...)`, in the same run. The comment there shows where:

1. `build_model_input(answer.question, answer.pages)` (in `model_input.py`) builds the prompt: an instruction to answer only from the numbered sources, paraphrase, cite each source by number, and not copy a source's wording, then the question and every page's passages as numbered sources.
2. Call your model with that prompt.
3. `check_model_answer(draft, answer.pages)` adds up the exact wording copied from each source in the draft (excerpts of 20 characters or more) and flags any source copied for more than 200 characters in total. The Terms of Use apply the same limit to verbatim text in model output, and a prompt alone doesn't guarantee it. Rewrite or drop a flagged draft.
4. Show the draft with its numbered sources and links next to it.

Treat retrieved content as untrusted data, not instructions, and keep a human review step: a questionnaire answer is a statement about a vendor. Because the app's own quote and any copied wording in the model's answer add up for the same source, keep the model to paraphrasing. Model costs, data handling, and retention are your application's responsibility. Don't add storage for passages or drafts.

## Data handling

This is a developer sample, not a ready-made procurement tool. Before you build on it, review the [Web IQ Terms of Use](https://www.microsoft.com/en-us/webiq/webiq-legal), including section 2.5, the Use and Display Requirements in Exhibit A, and section 3.2.15 on storing and caching. Not storing results doesn't on its own meet every use and display requirement.

The sample writes no result files, caches, or logs, calls no model, and sends no telemetry. It refuses to run when its output is redirected or piped. Terminal software, shell history, session recording, debuggers, and proxies can still keep data outside the sample's control, so don't record, redirect, log, or save retrieved content.

Questions and page URLs are sent to Web IQ, so use only text you're authorized to send. For how Web IQ handles requests, see the [Web IQ privacy guidance](https://learn.microsoft.com/en-us/web-iq/resources/faq#privacy-and-data-handling).

## Contribute

Install in editable mode, then run the offline tests. They use synthetic data and a mock service, so they need no key or network access:

```text
python -m pip install -e ".[dev]"
pytest
```

For a live check, run `webiq-vendor-questionnaire` in an interactive terminal with a key that has Browse access, and check the quotes, links, and crawl dates against the pages. Each run makes one billable request per question and page.
