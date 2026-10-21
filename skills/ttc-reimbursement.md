# TTC reimbursement

Use when the owner asks for a transit or TTC expense report for a period.

Route: `home` (the card site sees the usual IP). Workspace: desktop.

1. Ask the broker for the card-site login; wait for the owner's approval.
2. Open the statement page; download transactions for the period as CSV. Read-only: never open payments, transfers or settings.
3. `home_agents.jobs.ttc.parse_statement` → `ttc_charges(start, end)` → `write_xlsx`.
4. Save the statement pages covering those charges as PDF evidence.
5. `draft_email` to the saved recipient with both attachments; the owner approves or edits before it sends.
6. Log out, destroy the workspace, record the merchant rules and recipient for next month.
