# Explain LedgerLab

## A three-minute demo

1. Start `python3 -m ledgerlab.server --port 8011` and open the local dashboard.
2. Load the synthetic dataset. Show the captured, partially refunded, and refunded payments. The journal's signed balance remains zero.
3. Create a `2500`-cent payment and replay its request with the same idempotency key. Explain why a network retry should not produce another payment.
4. Request a `500`-cent refund, then an excessive refund. Show that the failed request leaves the accounting history unchanged.
5. Run reconciliation using the sample CSV. Identify the changed amount and the missing settlement row.
6. Run the tests. Point to a concurrency test and explain the invariant it verifies.

Use synthetic descriptions and amounts throughout. Reconciliation works on the current local payment snapshot; it does not download a real settlement report.

## Questions to answer in your own words

- Why does `0.1 + 0.2` in floating-point arithmetic motivate integer cents?
- What two records are posted for a payment, and what do those accounts represent?
- Why is a refund a new transaction instead of an edit to the original payment?
- Why must the idempotency check and accounting writes share one transaction?
- What could happen if two refund requests read the balance before either acquires a write lock?
- Why does replay return the original response even if the payment has since been refunded?
- How does a database transaction differ from a Python thread lock?
- Why does SQLite simplify this demo, and what workload would expose its limits?
- Why is a duplicate settlement row not automatically added to the first row?
- What does a zero journal sum prove? What does it fail to prove about whether the right customer or amount was recorded?

## Make the project your own

Choose one extension from the README. Write the behavior you expect, add a test that currently fails, implement it, and record the result in a short development note. A useful note describes the alternatives considered and the evidence that changed your decision. Do not claim production usage or performance numbers you have not measured.

The initial code was AI-assisted. Being able to explain and modify it is more useful than memorizing a description. Keep this distinction clear when describing which parts you designed, implemented, tested, or improved yourself.
