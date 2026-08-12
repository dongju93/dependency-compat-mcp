# Curated evidence pack

This directory is the product's centre of gravity, not a nice-to-have. Registry metadata
tells a caller whether a combination _installs_; only the entries here can tell them
whether a publisher _says it is supported_, and only these entries produce `changes`.
Coverage of this directory is, in practice, the coverage of the server
([03 §큐레이션 근거 팩](../../../../docs/03-compatibility-check-process.md)).

The pack ships **empty on purpose**. 03 is explicit: an entry count is not a completion
criterion, and an implementer must never turn a documentation placeholder into an entry or
invent evidence to make the file look populated. `entries: []` is a valid, honest pack. The
loader, the schema, lookup and the output conversion are all exercised by real fixture
packs under `tests/fixtures/packs/`.

## Files

| File                                                   | What it is                                                        |
| ------------------------------------------------------ | ----------------------------------------------------------------- |
| `compatibility.json`                                   | The pack envelope. Currently `{"pack_version": …, "entries": []}` |
| [`../runtime_releases.json`](../runtime_releases.json) | CPython and Node.js release + EOL snapshot                        |
| [`../loader.py`](../loader.py)                         | Validation. Every rule below is enforced there                    |

Every `*.json` file in this directory is loaded. They must all declare the **same**
`pack_version`: one repository state is one pack version, because `pack_version` travels
on each piece of evidence as `provenance` and is part of the cache key.

## The review contract

An entry is a claim that a **human read an official source and confirmed it**. That is the
only thing separating this data from a plausible guess, so:

1. **The source is read, not recalled.** Open the URL. Confirm the page actually says what
   the entry says. An external AI tool may produce a draft (03 permits that at contribution
   time, never at request time), but the draft is not evidence until a person has compared
   it against the source.
2. **`reviewed_by` is the person who did that comparison**, and `reviewed_at` is the day
   they did it. Both are required. Without them nobody can tell how stale an entry is or
   who to ask about it. CI warns when `reviewed_at` ages past the configured window.
3. **One source per statement and per change.** `source.url` is mandatory; an entry without
   one is rejected at load time, which means the server does not start.
4. **The URL host must be on the allowlist** in `loader.py` (`OFFICIAL_HOSTS`). Blogs,
   forums, Q&A and summary sites are not evidence. Adding a host widens what the whole
   server will accept, so it needs its own review — say in the PR why that host is the
   publisher of record for the project.
5. **Never weaken a `category`.** `breaking_change`, `removal`, `deprecation` and
   `migration_required` are the only four, and the choice is part of the review.
6. **A dead link is a maintenance issue.** A scheduled CI job HEAD-checks every
   `source.url`. It reports; it does not block the build. If a URL dies, the entry no
   longer satisfies 04's "a source the caller can verify themselves" and should be
   repointed or removed.

## Entry schema

```json
{
  "namespace": "pypi",
  "name": "example-framework",
  "applies_to": ">=5.2,<5.3",
  "verified_against": ["5.2", "5.2.1"],
  "reviewed_at": "2026-08-10",
  "reviewed_by": "REVIEWER_HANDLE",
  "statements": [
    {
      "stance": "supports",
      "counterpart": { "namespace": "runtime", "name": "python" },
      "expression": ">=3.10,<3.14",
      "scheme": "pep440",
      "source": {
        "source_type": "official_support_policy",
        "title": "Supported Python versions",
        "url": "https://docs.example.invalid/supported-versions"
      }
    }
  ],
  "changes": [
    {
      "category": "removal",
      "area": "removed_api",
      "summary": "The API was removed in this release.",
      "source": {
        "source_type": "official_release_note",
        "title": "Version 5.2 release notes",
        "url": "https://docs.example.invalid/releases/5.2"
      }
    }
  ]
}
```

| Field              | Rule enforced at load time                                                           |
| ------------------ | ------------------------------------------------------------------------------------ |
| `namespace`        | `pypi`, `npm` or `runtime`                                                           |
| `name`             | Parsed by that namespace's own name parser, same as tool input                       |
| `applies_to`       | A **range** in the namespace's scheme (PEP 440 for pypi/python, SemVer for npm/node) |
| `verified_against` | **Exact** versions, canonical spelling only. May be empty                            |
| `reviewed_at`      | `YYYY-MM-DD`, required                                                               |
| `reviewed_by`      | Non-empty, required                                                                  |
| `stance`           | `supports` or `excludes`                                                             |
| `scheme`           | `pep440` or `semver`; must match the counterpart's ecosystem                         |
| `source_type`      | `official_support_policy` or `official_release_note` — the only two a pack can use   |
| `category`         | `breaking_change`, `removal`, `deprecation`, `migration_required`                    |

Unknown fields are rejected. `(namespace, name, applies_to)` must be unique across the
whole directory, so which entry wins never depends on file order.

### `applies_to` versus `verified_against`

`applies_to` is where the statement is _claimed_ to hold; `verified_against` is where a
human _checked_ it. They are separate because 03 requires the difference to reach the
caller: a statement still applies outside `verified_against`, but the response carries
`curated_not_verified_for_version` so the caller knows how far the review reached.

## Adding an entry

1. Pick the exact release range the official statement covers and write it as `applies_to`.
2. List in `verified_against` only the exact versions you personally checked.
3. Add one `statements[]` entry per official support/exclusion statement, and one
   `changes[]` entry per documented breaking change, removal, deprecation or required
   migration. Copy the source's own title.
4. Run `uv run pytest tests/test_curated_loader.py`. A schema violation is a **start-up
   failure**, so a red loader test is a server that will not boot.
5. Open the PR with the source URLs in the description so the reviewer can check the same
   pages you did.

## Runtime release table

[`../runtime_releases.json`](../runtime_releases.json) is a separate, complete snapshot of
official upstream CPython and Node.js releases — **generated at 2026-08-12**, recorded in
its `generated_at` field. It is not a fixture: 03 forbids shipping a registered runtime
with only a handful of versions.

Regenerate it with:

```sh
uv run scripts/build_runtime_releases.py
```

The generator reads four first-party sources (python.org's download API, peps.python.org's
release-cycle data, nodejs.org's dist index, and nodejs/Release's `schedule.json`) and
writes a deterministically sorted, `indent=2` file so the diff stays reviewable. Review the
diff; do not hand-edit the file.

`eol_at` is the end of life of that version's release **line** — every `3.13.x` shares one
date, as does every Node `22.x`. It is `null` whenever upstream has not published a
day-precision date; several active lines currently publish only `YYYY-MM`, and padding a
month into a day would invent a fact. 03's lower-bound staleness check does not fire on a
`null` or a future EOL, so `null` costs nothing and claims nothing.
