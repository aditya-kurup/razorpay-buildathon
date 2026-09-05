# Reservation concurrency report

- Runs completed: **50**
- Overdraw count: **0** (target zero)
- Double-redemption count: **0** (target zero)
- Successful offers per run: `{"50": 50}`

The budget check and hold are protected by one manager-instance `RLock`, making the check-and-update indivisible for all shared in-process callers. Expiry is resolved synchronously under the same lock before reads and redemption, with no timer or sweeper thread.
