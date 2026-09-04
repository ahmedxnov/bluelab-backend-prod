# Repository notes

Three documents, and only three. Everything else lives upstream in the phase
folders at the repository root, because *"documentation that lags the system is
documentation that lies"* — and a second copy of an upstream decision is exactly how
that starts.

| Document | Answers |
|---|---|
| [open-items.md](open-items.md) | **Everything deferred, undecided, or routed upstream.** Read this before assuming something is finished |
| [module-boundaries.md](module-boundaries.md) | Why the packages are drawn where they are, and what makes the boundary hold |
| [call-plane-seam.md](call-plane-seam.md) | How this repository talks to `bluelab-agent-prod`, and the two places they currently disagree |
| [build-order.md](build-order.md) | What must exist before the first feature merge, and in what order the rest follows |

## The rule for open items

Anything deferred, assumed, or routed to the owner lands in
[open-items.md](open-items.md) **in the same diff that creates it**. Not in a
commit message — a commit message is archaeology, not a backlog — and not only in
a code comment, because a comment is invisible to anyone who is not already
reading that file.

The phase folders upstream all carry an "Open items routed to the owner" section
for the same reason. This is that section, for the implementation.

## The rule for adding to this directory

If the answer is already in `specs/`, `architecture/`, `stack/`, `api/`, `data/`,
`security/`, `quality/`, `observability/`, `pipeline/`, `infra/`, or `ux/`, **cite
it — do not restate it.** Restating owned content in another file is a defect in
that document set, and the same discipline applies here.

Docs land **with** the change, not after (quality/08 §1 item 7).
