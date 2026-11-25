# VendorIQ — Hackathon Submission Checklist

## Repository and documentation
- [x] Public GitHub repository created.
- [x] Application source, tests, and requirements committed to main.
- [x] Local secrets, databases, generated RFQs, and local storage excluded by .gitignore.
- [ ] Add final pitch deck to the repository or submission portal.
- [ ] Confirm README setup instructions on a clean environment.
- [ ] Add a demo video or screenshots if accepted by the hackathon.

## Quality assurance
- [x] Phase 13 release preparation was reported complete in the project development log.
- [x] Local full test suite passed: 231 tests (user-reported run on 25 Nov 2026; 44.72 seconds).\n- [x] GitHub Actions test workflow passed for commit 038f0b03 (25 Nov 2026).
- [ ] Verify Phase 14 UX tests and all dashboard views in a browser.
- [ ] Verify a clean first-run experience with an isolated database (Phase 13 QA previously reported successful; repeat on current commit).
- [ ] Verify upload size/type validation and unsupported file handling.
- [ ] Verify RFQ and quotation workflows using synthetic data.
- [ ] Confirm no secrets or private data are present in the public repository.

## Demonstration
- [ ] Prepare a clean demo database with synthetic records (runbook and quotation scenario are documented; supplier bootstrap is still pending).
- [ ] Rehearse docs/DEMO_SCRIPT.md and docs/DEMO_RUNBOOK.md in a browser.
- [ ] Show evidence provenance and candidate status.
- [ ] Show that technical compliance is non-compensable.
- [ ] Show that missing landed-cost inputs are disclosed.
- [ ] Show the human approval gate.
- [ ] Avoid claiming supplier approval or certification without independent proof.

## Deployment
- [ ] Check current free-tier limits and terms before choosing a host.
- [ ] Confirm the app can run with offline fallback and no paid API.
- [ ] Configure secrets through the host's secret manager, never in Git.
- [ ] Configure persistent storage for SQLite or use an appropriate managed database.
- [ ] Test restart/redeploy behavior and data persistence.
- [ ] Record a live URL only after successful deployment and smoke testing.

## Final submission
- [ ] Verify official Homingo submission requirements and deadline in the participant portal.
- [ ] Submit the repository URL.
- [ ] Submit a deployed demo URL if required and available.
- [ ] Submit pitch deck/demo video if required.
- [ ] Save submission confirmation.

A checked item means the action has been completed based on the current project record. Pending items must not be represented as complete.
