# Weekly programmes from Home Assistant

Supported IoT convectors configured for a **weekly programme** can activate,
pause and edit their seven saved intervals without the vendor app or panel.
These operations still use Eldom's cloud; this is not local control.

Choose **Auto** on the climate entity to activate the existing programme.
Choose **Heat** to use Manual mode: the integration remembers which intervals
were enabled in the config entry's options, then clears their activation flags.
Times, days and temperatures remain in the radiator. Auto restores those flags,
including after an integration reload or HA restart. Off powers the radiator off;
turning it on resumes the mode determined by its current interval flags.

The remembered flags are used only while the radiator's interval contents still
match the saved profile. If another client changes them, explicitly choose the
intervals in the activation action. An empty programme is never guessed.

## Native actions

All three actions target the Eldom climate entity, have translated forms in
Developer tools → Actions, and optionally return a response containing the mode,
all seven intervals, and the remembered enabled interval numbers.

- `eldom.get_weekly_schedule`: read the current programme.
- `eldom.activate_weekly_program`: activate it; optional `enabled_intervals`
  explicitly selects a nonempty list of interval numbers (1–7).
- `eldom.set_weekly_interval`: change one interval. Only `slot` and at least one
  changed field are needed; omitted fields and the other six rows are retained.

```yaml
action: eldom.set_weekly_interval
target:
  entity_id: climate.example_convector
data:
  slot: 1
  enabled: true
  weekdays: [monday, tuesday, wednesday, thursday, friday]
  start: "08:00"
  end: "16:00"
  temperature: 15.5
```

```yaml
action: eldom.activate_weekly_program
target:
  entity_id: climate.example_convector
data:
  enabled_intervals: [1]
```

Weekdays use full English identifiers (`sunday` through `saturday`); the form
translates their labels. Times have whole-minute precision. Temperature supports
5–35 °C in 0.5 °C steps. Enabled intervals require at least one day and a start
before the end on the same day; use separate intervals around midnight.

Editing while Off never turns the radiator on. Editing times, days or temperature
while paused in Heat keeps the programme paused and updates its remembered
contents. Enabling an interval explicitly can resume the programme when powered
on. Disabling the last remembered interval deliberately leaves no programme to
reactivate. Auto may turn an Off radiator on.

## Validation and limits

The implementation reads all 35 interval fields before writing a complete
`SetTimers` request and independently confirms all seven rows and the operating
mode. Requests share the connection's operation lock and a 45-second timeout.
Writes are sent once; uncertain acknowledgements are not automatically replayed.
Other configuration parameters are checked for unintended changes. Readback
temperatures use tenths of a degree while writes use degrees.

Turn sleep off before editing, pausing or activating a programme. Timer-configured
devices (`SystemSettings` first component `0`) cannot be converted to weekly
configuration through these actions. Legacy convectors do not support the new
actions. Off/On continue to work independently of the weekly-program reader.

Protocol tests cover preservation, invalid input, partial failures, persistence,
concurrency and HA service registration. Hardware validation on a 3000 W IoT
convector confirmed pause/activation, disabled-row edits and Off behaviour, with
the original programme and settings restored after each test.
