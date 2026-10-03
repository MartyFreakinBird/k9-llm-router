# K-9 repository hardening: amended audit blueprint v1

Date: 2026-10-01
Authority: owner directive recorded in orbitron-integrator commit `72ea3140925af2dc783e95486ace08130ca55e43`.
Disposition: proposed only. Owner audit is required before execution. No tracked files, imports, branches, deployments or wallet pointers are changed by this artifact.

## 1. Claims deliberately refused

- Base44 is a validator, not the generator or issuer of the sovereign verification key. The homelab is the sole VK-generation authority for this plan.
- No claim that `bb` is runnable here, that a historical successful build remains installed, or that a new directory layout changes this capability boundary.
- No claim that a local Foundry pass is a testnet verification receipt, that a witness is a proof, or that circuit satisfaction establishes complete governance/alignment binding.
- No claim that the orphan verifier is authoritative because it compiles, has the right public-input count, or appears in an auto-commit.
- No claim that an object-looking hexadecimal string proves the Git object exists.
- No invented circuit tag, successor deployment address, testnet receipt, toolchain revision or production pointer state.

Current validator measurement: glibc 2.36; no `bb`, `forge` or `nargo` executable found in this run's PATH. Solidity/VK comparison and manifest checks can be implemented here, but no such script or Foundry result is claimed to have run in this blueprint turn. If a validation tool is unavailable, that gate remains pending or runs in an identified validator environment with its evidence returned. Nothing silently becomes approved.

## 2. Evidence and repository scope

The directive lives in `MartyFreakinBird/orbitron-integrator`; the circuit/verifier hardening concerns `MartyFreakinBird/k9-llm-router`. These are separate repositories. Fetching the directive does not authorize pushing either repository.

Owner/relay measurements: duplicate circuit byte-identical today; stale `master=7c528b0` is the merge-base with `main`; orphan provenance includes `eaf4978`. A read-only comparison of the available nested sandbox circuit snapshot also found equality. Cached sandbox branch refs are not fresh remote evidence and do not supersede the owner measurement. Before execution, identify a clean target clone, verify its remote identity and refresh refs. Resolve full object IDs there.

Known regression vector: amount_in=1000000001, predicted impact=12 bps, floored deduction=1200000, min_amount_out=998800001. Reported homelab artifacts: proof=8384 bytes, public_inputs=2272 bytes (71 32-byte words), VK=1888 bytes; generated Solidity VK hash matches artifact `0x00817d586f57004beafe501552889901c4f442a1c4d7cf20c8ffdb8754a35715`. The attached Foundry trace was checked and returned true for the successor regression proof. These facts are specific to that run, not a general source-release or testnet approval.

## 3. Named survivor and proposed final layout

Sole trade circuit authority: `contracts/aeg9_circuit/`, with source `contracts/aeg9_circuit/src/main.nr`. Its exact source revision will be pinned by an owner-approved tag referencing a verified commit. No tag name or existence is presumed.

Proposed authoritative homes after approved execution:

- `contracts/aeg9_circuit/src/main.nr`: surviving circuit source.
- `contracts/aeg9_circuit/Nargo.toml`: circuit configuration.
- `contracts/aeg9_circuit/lineage.json`: machine-written accepted lineage manifest.
- `contracts/src/ProofOfRationalityVerifier.sol`: sole importable verifier source, replaced or retained only according to verified provenance.
- `contracts/test/fixtures/aeg9/<lineage-id>/`: non-secret proof/public-input/VK fixtures and their checksum manifest, with narrow Foundry read permission.
- `scripts/aeg9/`: proposed homelab generation and validator-only checks. Their names and implementation are proposed, not existing capabilities.

Disposable `target/` remains build output, not a second source authority. Release-attestation circuits are a separate purpose and must not be silently folded into trade-circuit authority or represented as equivalent.

Explicit duplicate disposition:

1. `contracts/circuits/proof_of_rationality.nr`: compare source bytes and inspect references at the execution revision, record both hashes and provenance, then explicitly delete in the approved deduplication commit. If equality no longer holds, stop and audit the difference before deletion. Today's danger is drift, not established divergence.
2. Orphan `contracts/src/ProofOfRationalityVerifier.sol`, associated with `eaf4978`: extract its embedded VK hash, inspect all available generated VK data and compare against homelab-authoritative lineage. Record mismatch/match/unknown, source blob hash and commit provenance before any replacement, relocation or deletion. A textual hash match alone is not a cryptographic verifier-equivalence proof; generated source digest and functional checks must also agree with the homelab artifact. If it is the accepted successor, preserve this canonical home and record lineage. If it belongs to the old key, preserve historical evidence and replace only in the appropriate approved sovereignty-chain change. If provenance is unresolved, stop. No deletion of the last usable verifier.
3. Additional generated or nested duplicates: inventory and name them in an amended audit before adding them to deletion scope. This artifact does not authorize broad cleanup.

## 4. Machine-written lineage contract

A proposed homelab generation script must compile, generate the witness, generate proof/VK/verifier with explicit EVM settings and write lineage as one checked pipeline. It parses the fresh nargo artifact itself. It must never populate circuit-derived fields from manually copied documentation.

Required manifest fields:

- version, repository identity, verified full circuit_commit, relative circuit path and source/configuration content hashes;
- compiler version, compiled artifact noir_version, ABI and bytecode/artifact hashes;
- witness_schema derived from the artifact ABI, including visibility, types and field/array structure;
- recursively flattened public-input layout and field count derived from the same ABI, in declaration order;
- generation run identifier, homelab generator identity, commands/settings, actual nargo and bb version/build evidence (a placeholder bb version is explicitly insufficient);
- witness/proof/public-input/VK/raw-vk-hash/Solidity-source digests and file lengths, distinct raw artifact hash and embedded protocol VK hash fields;
- separate verifier total-count, external-input-count and pairing-point treatment, measured from generated outputs rather than assumed universal constants;
- validation results and evidence digests, with local verification and testnet verification explicitly separate;
- predecessor lineage and provenance dispositions where applicable.

Resolve circuit_commit using `git rev-parse --verify <sha>^{commit}`, record the resolved full object ID, and ensure the source tree used actually matches that revision. Require a clean relevant source/configuration tree and bind dependency versions; a commit existing does not establish that uncommitted files match it. Validate any declared tag resolves to that same object.

Write the completed manifest through a temporary file and atomic rename only after all required generation-stage checks pass. Remove or mark incomplete outputs from failed runs; never publish a partial manifest as accepted lineage. A digest detects mutation but does not authenticate authority: acceptance also needs an owner-approved homelab trust anchor/receipt and reproducible correspondence to the accepted source revision. No trust in a self-described generator field alone.

## 5. Validator responsibilities and gates

Base44-side checks are read-only with respect to authoritative artifacts: validate manifest structure, resolve Git identities, recompute file digests, compare ABI-derived layout, inspect embedded VK data and compare against the approved homelab lineage. A proposed `verify_vk_hash.js` must be implemented/reviewed before claiming its result; its name in a directive is not execution evidence.

Required validation gates:

- no mixed-run source, witness, proof, public inputs, VK or verifier;
- matching manifest and actual fixture digests; malformed/trailing public-input bytes rejected;
- expected input layout derived from the actual artifact; do not confuse 79 total verifier fields with 71 external inputs for this known run;
- successor accepts the unmodified regression proof;
- mutations of valid field-valued public inputs and corrupted proof bytes are rejected (false or appropriate verifier revert). Mutation tests must establish the original fixture passes and must not count arbitrary setup/path errors as rejection evidence;
- checks for private-range ceilings and integer-floor regression vectors;
- wallet guards use exact errors and genuine success expectations rather than swallowing unrelated reverts;
- full suite scope explicitly reported, including the legacy missing-fixture test until repaired or deliberately retired with reviewed provenance;
- import/reference preflight, duplicate-authority checks and build/test results after approved restructuring.

These gates do not repair known limitations such as unused public commitments or simplified wallet valuation. Record those separately; do not label the stack fully aligned or production-approved from the regression pass.

## 6. Two strictly partitioned execution tracks

### Track A: sovereignty transfer, existing locations

Homelab generates authoritative VK and successor verifier; validator checks lineage and positive/negative evidence. Existing import paths stay frozen while the transfer is in flight. Deploy successor independently, confirm Base Sepolia chain ID and actual deployed bytecode/linkage, then verify the accepted regression proof against that deployed successor. Require address, receipt/call evidence, proof/public-input digests, block identity and returned success. Only then may the owner-authorized wallet `setVerifier()` step occur, followed by read-back confirmation.

No `setVerifier()` is implied or performed by this plan. Failure leaves the predecessor pointer unchanged. Source-generation changes required for Track A belong to Track A, not a disguised restructure commit.

### Track B: repository hardening, separate commit series

Entry conditions: owner audit of this artifact; fresh target identity inventory; accepted lineage/provenance; Track A no longer in flight, or an explicit partition approved that guarantees no overlapping authority files/import moves. This blueprint selects the conservative default: no import/path restructure until Track A completion evidence is available.

Proposed commit sequence:

1. Audited plan, validator preflight and machine-lineage implementation without moving authority paths.
2. Adopt verified homelab lineage and durable non-secret test fixtures; reconcile legacy test coverage.
3. Explicit duplicate-source removal and orphan disposition, with before/after hashes and reference checks.
4. Any separately reviewed path/reference cleanup, with full applicable tests and no change of cryptographic semantics.
5. Retire stale `master` after a fresh independent branch check.

Every commit carries its own acceptance report. No push until the standing :9003 Proof-of-Alignment requirement and applicable release-bound gates are satisfied. Tests or static health responses are not substitutes for those gates. Preserve cross-module contracts, K9-CB boundaries and endpoint routing; this hardening work is not permission to change the operational architecture.

## 7. Stale master retirement

Owner-reported expected tip: `7c528b0`, not `d3f2bb2`. During the approved restructure window, refresh both remote refs, resolve the reported object fully and verify it remains an ancestor of current main, is the expected merge-base, and has zero commits unique to master. Identify the repository and remote explicitly.

If any fresh result differs, stop branch deletion and report the difference. No force-push, no discarding unique commits. Record the old full tip and ensure recoverability before removing the branch. Confirm repository default branch and consumers do not require master. The user instruction to cut stale master is retained, but has not been executed.

## 8. Rollback and evidence preservation

Before future mutations, capture source blob hashes, accepted manifests, verified refs and predecessor deployment identity. Preserve proof/VK provenance without copying secrets. Work in a clean dedicated branch; do not mix user changes or nested sandbox copies into commits.

Failed hardening tests: stop and revert only the affected reviewed hardening change; do not reset sovereign artifacts or alter live pointers. Failed successor testnet verification: retain predecessor pointer, retain failure evidence and investigate. A live pointer rollback is a separate owner-authorized operation with current contract permissions checked, not an automatic script action.

## 9. Owner audit checklist

- [ ] Homelab-only generation authority and Base44 validator limitations accepted.
- [ ] Repository identities and named survivor accepted.
- [ ] Machine-derived lineage fields and authority authentication accepted.
- [ ] Orphan verification-before-disposition and explicit duplicate deletion accepted.
- [ ] Track A / Track B partition and frozen import boundary accepted.
- [ ] Positive/negative, full-suite and testnet evidence gates accepted.
- [ ] Fresh ancestry check before cutting master accepted.
- [ ] Rollback and standing pre-push alignment gates accepted.

Until owner audit is complete: artifact only. No hardening execution, source relocation, import rewrite, duplicate deletion, branch deletion, push, deployment or verifier repoint.
