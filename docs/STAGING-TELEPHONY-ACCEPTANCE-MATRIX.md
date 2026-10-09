# Staging Telephony Acceptance Matrix

Not a new phase -- a consolidated, evidence-cited matrix over Phases
2.19-2.32 and 2.35, built by reading those phases' own headline/results
sections (cited per row), not by re-running anything. Purpose: tell
previously-verified-real-staging apart from hermetic-only and from
genuinely not-yet-attempted, so the next real-staging session knows
exactly where to start rather than re-proving ground already covered.

Grades used: **REAL-GREEN** (run against real staging infrastructure,
passed), **REAL-PARTIAL** (run against real staging, passed with a
documented, bounded limitation), **HERMETIC-ONLY** (proven by unit/
integration tests against fakes, never against real staging),
**NOT-ATTEMPTED** (no run exists, real or hermetic, for this exact
scenario).

| Capability | Grade | Evidence |
|---|---|---|
| SIP INVITE / signaling | REAL-GREEN | `PHASE-2.25-REAL-SIP-SPOKEN-E2E.md` §3 ("GREEN, with three real bugs found and fixed") |
| RTP, both directions (transport layer) | REAL-GREEN | 2.25 §3; reconfirmed unchanged in `PHASE-2.26-REAL-SIP-TTS-PLAYBACK.md` §1 |
| Real hangup | REAL-GREEN | 2.25 §8 |
| Multiple sequential real calls | REAL-GREEN | 2.25 §9; `PHASE-2.30-LONG-RUNNING-STABILITY.md` §3 |
| Media WebSocket attach (`uuid_audio_stream`) | REAL-GREEN | `PHASE-2.24-REAL-MEDIA-CALL-E2E.md` §2, §5 |
| Media WebSocket message-size bound (`max_size`) | HERMETIC-ONLY | This session's own Phase 2.45 fix -- `tests/telephony/freeswitch/test_media_transport.py`, never exercised against a real FreeSWITCH frame size |
| Real caller speech -> real Deepgram STT | REAL-GREEN | 2.26 §1 headline |
| Real OpenAI LLM response | REAL-GREEN | 2.26 §1 headline |
| Real Deepgram Aura TTS synthesis + delivery to real SIP caller | REAL-GREEN | 2.26 §1 headline ("single call, reproduced 3 times") |
| Complete one-shot spoken round trip | REAL-GREEN | 2.26 §1 |
| 2-way real concurrent spoken calls | REAL-PARTIAL | 2.26 §1 ("isolation confirmed; audio quality and a test-harness race are documented limitations") |
| 3-4-way real concurrent media | REAL-PARTIAL | `PHASE-2.27-CONCURRENT-MEDIA-VALIDATION.md` §6 (one recurring single-word STT miss under load, root-caused, not fully eliminated) |
| Concurrent AI-session isolation | REAL-GREEN | `PHASE-2.28-CONCURRENT-AI-QUALITY-VALIDATION.md` §7 |
| Live cancellation during active AI/media work | REAL-GREEN | 2.28 §8; `PHASE-2.29-REAL-MEDIA-FAILURE-STABILITY.md` §3 |
| Live media-disconnect during concurrent AI work | NOT-ATTEMPTED | 2.28 §9 -- explicitly not attempted, documented why (harness limitation) |
| Concurrent failure isolation (one call's failure doesn't affect others) | REAL-GREEN | 2.28 §10; 2.29 §4 |
| 8-way / 10-way real concurrent groups | REAL-GREEN, one defect found+fixed | `PHASE-2.32-SUSTAINED-CONCURRENCY-VALIDATION.md` §3-§7 (a real teardown defect found under 10-way load, fixed, regression-tested) |
| Concurrency beyond 10-way | NOT-ATTEMPTED | 2.32 §9a: "10-way was the largest concurrency this session safely established... not pushed further" |
| Resource-exhaustion (deliberately pushed past capacity) | NOT-ATTEMPTED | No phase runs calls *to failure* under load -- every real-staging concurrency phase (2.27-2.32) stayed within a safely-completing envelope; §9a of 2.32 explicitly leaves the RSS trend "not resolved," never "ruled in" by pushing further |
| Long-running single-call stability (soak) | REAL-GREEN | 2.30 §3, §5, §6, §7 |
| Repeated failure -> recovery | REAL-GREEN (+ hermetic) | 2.30 §5, §7 |
| Listener-level disconnect (one connection drops) | REAL-GREEN | `PHASE-2.31-LONG-RUNNING-RUNTIME-VALIDATION.md` §7 |
| Listener-wide restart/recovery (`Server.close()` + rebind + fresh call succeeds) | NOT-ATTEMPTED | Phase 2.35 script exists and is lint/type-clean (`docs/PHASE-2.35-LISTENER-RESTART-RECOVERY.md`) but has never been executed -- needs the same real staging FreeSWITCH this matrix's other REAL-GREEN rows used, not available in this sandbox |
| Tenant isolation / security under real concurrent load | REAL-GREEN | 2.30 §9; `PHASE-2.31...` §9 |
| Provider idle-timeout / cancellation (hermetic) | HERMETIC-ONLY, thorough | `tests/providers/test_pipelined_engine*.py` (4 files, incl. dedicated error-handling and hardening suites) -- never re-run against a real provider timing out live, since every real-staging phase above used real, responsive providers |
| Cross-provider fallback | NOT APPLICABLE | No fallback mechanism exists in this architecture (ADR-0009 names none) -- nothing to test |
| Backup/recovery against real disposable Postgres | CI-ONLY, UNVERIFIED | Phase 2.43's `recovery-drill` CI job -- never actually executed (see this session's CI report); passes locally only insofar as its own hermetic unit tests do |
| Off-host backup against a real remote destination | NOT-ATTEMPTED | `docs/RUNBOOK-OFFHOST-BACKUP-ACCEPTANCE.md` -- blocked on a real S3/SSH credential, not available in this sandbox |

## Reading this table

Nothing above was re-run to produce this matrix -- every REAL-* grade is a
citation into an existing phase doc's own already-completed real-staging
work, not a new claim. The three genuinely open real-environment gaps,
in priority order for whoever next has staging access:

1. **Phase 2.35 execution** -- code exists, zero infra beyond what 2.21-2.32
   already used.
2. **Deliberate resource-exhaustion** -- push concurrency past 10-way
   specifically to observe failure behavior, not just to confirm success
   (every prior concurrency phase stopped at "safely completed," never at
   "and here is what happens when it doesn't").
3. **Off-host remote-storage runbook** (`RUNBOOK-OFFHOST-BACKUP-ACCEPTANCE.md`)
   -- needs a real S3 bucket or SSH host, not re-derivable from this
   sandbox.
