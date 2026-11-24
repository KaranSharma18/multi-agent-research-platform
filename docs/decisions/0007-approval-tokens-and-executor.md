# 0007. Writes need a typed proposal, a policy check, a person, and a separate executor

**Context.** Opening issues and posting to Slack are the point of "research *and action*", and also the
easiest way for a mistake or an attack to cause harm.

**Decision.** A chain where each link is checked by something different:

1. **Proposal (model):** typed `ActionProposals` from the *verified* answer only.
2. **Policy (code):** schema, allowlists, PII, link stripping, assignee provenance, rate limits. Rejections
   are stored with a reason.
3. **Approval (person):** the graph interrupts; an approver sees the exact preview. Approving re-hashes the
   stored arguments and signs a one-time Ed25519 token bound to run, action, tool and argument hash,
   expiring in 10 minutes. High-risk actions need someone other than the requester.
4. **Execution (separate process):** the executor, which never reads untrusted text, holds the only
   write-scoped credentials. The actions server re-verifies the token, burns its `jti`, and uses an
   idempotency key so retries never double-write.

**Why.** No single failure is enough. A compromised model can propose, but code checks it. A sloppy approval
can only approve what was shown, byte for byte. A leaked approval token works once, for one action, for ten
minutes. And the process that reads untrusted text holds no write credentials at all.

**Trade-off.** Writes are slower (a person has to look) and the system has one more service. Both are
intended: actions in this domain are rare and consequential.
