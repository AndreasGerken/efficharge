# Architecture

How the parts of efficharge fit together: a map for finding your way in the code. The
details live in the code and its tests.

## 1. Big picture

```
                ┌──────────────── one Python process (uvicorn, exactly 1 worker) ────────────────┐
                │                                                                                 │
 Energy-Charts ─┼─► EnergyFetcher ──► energy_data ──("new prices")──┐                            │
 (15-min grid)  │                                                    ▼                            │
                │                                     Supervisor ──► DeviceRunner (1 per plug)    │
 Browser ───────┼─► web (FastAPI + Jinja + HTMX) ──(commands)──►        │   pure core:            │
 (HTMX, charts) │      ▲ reads state and plans from the DB              │   machine.step(...)     │
                │      └────────────────────────────────────────────────┤   planner.best_window() │
                │                                                       ▼                         │
                │                                   AccountSession (1 per vendor account)         │
                │                                   └─ DeviceAdapter (Meross, simulated)          │
                └──────────────────────────────────────────────┬──────────────────────────────────┘
                                                               ▼
                                                    Meross cloud (HTTP + MQTT)
```

- **SQLite (WAL) is the single source of truth.** The web layer never talks to a plug: it
  sends commands to the supervisor and reads state from the database.
- **Exactly one worker process.** The device workers live in the web process; two workers
  would mean two supervisors switching the same plug.
- **Small on purpose:** no Redis, no Postgres, no numpy. The planner is pure Python and
  charts are drawn in the browser, so the process stays well under 200 MB of memory.

## 2. The key rule: pure core, thin shell

Every decision is a pure function, without I/O, clock or database:

```python
# control/machine.py
def step(state: DeviceState, event: Event, ctx: Context) -> tuple[DeviceState, list[Action]]
```

- **Events:** `PowerSample`, `SwitchState` (the relay changed), `Tick`, `Startup`,
  `Replan`, `ManualPlugIn`, `ChargeNow`, `ReadyBy`, `StopCharging`, `PlugOn`
- **Actions:** `TurnOn`, `TurnOff`, `StartCycle`, `SavePlan`, `RecordOverride`,
  `MarkCharging`, `FinishCycle`

The `DeviceRunner` is the shell: it collects events (power polls every 30 s, pushes from
the plug, commands from the web), calls `step`, carries out the actions through the
adapter and stores the new state in one transaction.

Because of this, the tests need no mocks (feed events, check the actions), and the demo
and the virtual plugs run the same logic with a simulated plug and their own clock.

## 3. Modules

| Package | What it does |
|---|---|
| `energy/` | Async client for the [Energy-Charts](https://www.energy-charts.info) API (day-ahead price, green-power forecast, CO₂ intensity), a fetcher every 15 min, and `EnergySeries`: a step function on a 15-min UTC grid. New day-ahead prices (around 13:00) trigger a replan of every waiting plug. |
| `planner/` | Pure. `window.py` picks the charging window, `deadlines.py` resolves the deadline rules, `duration.py` learns the charge time (section 5). |
| `control/` | `machine.py` (the pure `step`), `runner.py` (one asyncio task per plug), `supervisor.py` (starts and restarts runners, holds one `AccountSession` per vendor account, so one Meross connection per account), `savings.py`, `maintenance.py` (cleans up and finalises figures every 6 h). |
| `devices/` | The `DeviceAdapter` interface with capabilities (switch, switch events, power meter), the Meross adapter (`meross_iot`) and a simulated plug with a battery model. |
| `web/` | FastAPI routes, server-rendered Jinja templates, HTMX for live parts, Chart.js fed by `/devices/{id}/chart.json`. `explain.py` turns states and plan reasons into one plain sentence. |
| `db/` | SQLAlchemy 2.0 (async, `aiosqlite`) with Alembic migrations. All timestamps are UTC. |

The main tables: `users`, `device_accounts` (encrypted vendor token), `devices`,
`device_settings`, `deadline_rules`, `device_state`, `cycles` (one charge with its plan,
overrides and savings), `power_samples`, `energy_data` and `visit_counts` (anonymous page
views and visitors per day, for the admin page).

## 4. The state machine

```
             power above threshold (2 samples)
   IDLE (on) ──────────────────────────────► plan the charge
     ▲                                          │ starts within 10 min   │ starts later
     │                                          ▼                        ▼
     │                                     CHARGING (on) ◄─ at start ─ PLANNED (off)
     │                                          │                        │ new prices, settings,
     │    no power for 10 min                   │ battery full           │ "ready by" → replan
     └──────────────────────────────────── DONE (on) ◄───────────────────┘ "charge now" → CHARGING
```

Rules that came from running the first prototype:

- **Only the start is delayed.** Once charging, efficharge never switches off on its own.
- **Debounced:** a plug-in needs two readings above the threshold; "full" needs the power
  to stay low for 5 min (chargers dip briefly), or a clear switch-off out of the declining
  phase.
- **Blind while waiting:** in PLANNED the plug is off, so unplugging can't be seen. It
  shows at the start (no power after switching on → "unplugged").
- **When in doubt, on.** After a restart, `Startup` reconciles the stored state with the
  plug: unknown or unreachable means on. Removing a plug or an account switches it on
  first. Better charged inefficiently than not at all.

### The plug's button: never fight the user

| State | Button turns the plug… | Reaction |
|---|---|---|
| PLANNED (off) | on | charge now, recorded as an override |
| CHARGING (on) | off | `PAUSED_BY_USER`: stays off until the user turns it on again |
| IDLE / DONE (on) | off | `OFF_BY_USER`: no plug-in detection until it is on again |

Meross pushes relay changes (`togglex`), so a press arrives within about a second. The
runner remembers what it commanded for 15 s: a matching report is its own echo, anything
else is the user.

## 5. Planning

- **Windows** are contiguous (chargers restart their charge profile when interrupted)
  and start on the 15-min grid or now. If the best one starts within 10 min, charging
  starts right away.
- **Score:** a weighted mix of the normalised price and the non-renewable share. The
  user picks 🌱 Eco first, ⚖️ Balanced or 💶 Money first; the planner also computes the
  window of the other priority, so the dashboard can say whether both agree.
- **Deadlines are rules:** "Mon–Fri, plugged in 08:00–15:00 → ready by 18:00", checked in
  order, first match wins, otherwise "ready within 12 h". "Ready by" means the next
  occurrence of that time in the user's time zone. A one-off "ready by" beats the rules.
- **Charge time:** the user's estimate counts as a virtual charge; real charges (the
  last 20 that ended full) join it, and the planner takes the 90th percentile. So the
  battery is ready in about 9 of 10 cases, and the plan moves smoothly from the guess to
  the measured time.
- **No data, no time:** without prices, or without time to wait, the plan is "start now".
  Every plan carries a `reason` code, which the web layer turns into one sentence
  ("Charging now: it needs about 3 h 30 min and should be ready by 07:30").

## 6. Savings

Each charge is compared with charging right at plug-in. The measured energy per 15 min,
priced at the exchange price, gives the money saved; it is real money with a dynamic
tariff, since fees and taxes are the same either way. CO₂ uses the grid's measured
intensity (forecast until it is known); the maintenance job recalculates a charge once
its energy data can no longer change. Overrides count honestly, usually as about zero.

## 7. Cross-cutting

- **Time:** UTC in the database and the core; the user's time zone only in deadline
  rules and on screen. Tests cover the DST days (92 and 100 quarter-hours).
- **Vendor logins:** the Meross password is used once to get a token and never stored.
  Tokens are encrypted with AES-GCM, bound to their database row, with a key outside the
  database (so a backup alone exposes nothing).
- **Web security:** argon2id passwords, `Secure`/`HttpOnly`/`SameSite=Lax` session
  cookie, CSRF tokens on every form, a strict content security policy (no inline
  scripts), rate limits on login, registration and the public demo, and no access log.
- **Translations:** German and English through gettext. The core returns codes, never
  text; only the web layer translates. Numbers, money and dates are formatted with Babel.
  `tests/test_i18n.py` fails if a German translation is missing.
- **Mail:** optional, over the SMTP server of a mailbox (`mail.py`, TLS only). For now
  one use: a mail to the admins when someone signs up, at most hourly and without personal
  data (`web/signups.py`).
- **Fail-safe:** errors and lost connections leave the plug on (section 4).
- **Data:** Energy-Charts, prices from Bundesnetzagentur | SMARD.de, CC BY 4.0, credited
  in the footer.

## 8. What's next

More plugs (Shelly and Tasmota over MQTT, Home Assistant, a small local bridge for plugs
without a cloud API), a public API so a home system can ask "should I charge now?", and
push notifications. The adapter interface and the pure core are built for that: an API
client would feed the same `step` function as a plug does.
