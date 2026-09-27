# Governance

gitmemory is a young project with one maintainer. This document says how
decisions are made today and how that is meant to change as the project grows.
It will be revised as that happens, and every revision goes through a pull
request like any other change.

## Roles

- **Users** run gitmemory. Their bug reports and results are the most valuable
  contribution there is.
- **Contributors** send issues, reviews, docs or code. Anyone can be one; see
  [CONTRIBUTING.md](CONTRIBUTING.md).
- **Maintainers** are listed in [MAINTAINERS.md](MAINTAINERS.md). They review
  and merge pull requests, triage issues, cut releases, and handle security
  reports and Code of Conduct reports.

## How decisions are made

- **Day to day:** by lazy consensus on the pull request or issue. A change that
  passes CI and has a maintainer's approval is merged, unless someone raises an
  objection that has not been answered.
- **Changes to a locked decision** in [docs/DESIGN.md](docs/DESIGN.md), to the
  store format, or to this document start as an issue, stay open for at least
  one week, and record the decision and its reasons in the issue.
- **When consensus fails:** while there is a single maintainer, that maintainer
  decides and writes down why. Once there are three or more maintainers, a
  majority of them decides.

## Becoming a maintainer

A contributor who has made sustained, high-quality contributions (code,
review, or triage) can be nominated by any maintainer. With a single
maintainer, that maintainer's agreement is enough. With several, the nominee
needs a majority of maintainers and no unresolved objection. The project aims
to have maintainers from more than one organisation.

A maintainer who is inactive for six months can be moved to emeritus status,
and restored on request.

## Releases

Releases follow [Semantic Versioning](https://semver.org/). Every release is
recorded in [CHANGELOG.md](CHANGELOG.md). Until 1.0, a minor version may change
the store format; when it does, the changelog says so.

## Code of Conduct and security

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) and [SECURITY.md](SECURITY.md).
