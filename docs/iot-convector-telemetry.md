# IoT convector hourly telemetry

IoT (`HTRCNV`) convectors can expose the same hourly electricity and temperature
graphs as the official Eldom app. Support was checked against the app's 3.4.0
HTRCNV graph implementation and tested with Home Assistant 2026.10 and
pyeldom 2.1.1.

## Sensor and attributes

Each discovered IoT convector gains an **Estimated Energy Today** sensor in kWh,
attached to the existing device. Home Assistant assigns its entity ID; do not
assume an ID from the device's display name. It has no `state_class`: this daily
estimate is not a verified cumulative electricity meter.

Its attributes are:

| Attribute | Meaning |
| --- | --- |
| `energy` | Available dates, daily estimated `total` in kWh and 24 `hours` entries |
| `temperature` | Available dates and 24 `hours` entries, already in degrees Celsius |
| `source` | `Eldom (estimated)` |
| `retrieved_at` | Time of the successful snapshot, including timezone offset |
| `power_class` | Second component of the device's `SystemSettings` |
| `reference_power_w` | Power used to interpret the estimate, not instantaneous consumption |
| `power_regulation` | Read-only regulation status, or null when unsupported |
| `vendor_timezone` | Timezone value reported by the device |

Dates are calendar labels; hour positions are the manufacturer's 24 bins, not
UTC instants. DST days must not be expanded into invented 23/25-hour histories.
The Home Assistant and device timezones should agree. Today's current and future
hours are null; the vendor's daily total can already include the current hour.

Missing, malformed or unavailable values remain null. A zero actually returned
by the device is preserved. Missing days are not filled. The device controls
which history is available, up to seven dates; the integration does not import
past samples into Recorder or create long-term statistics from these estimates.

## Calculation

`GetEnergyStatistic` returns `Date`, `TotalPower` and `P0` through `P23`.
The official app multiplies these counters by a factor selected from the second
comma-separated component of `SystemSettings`:

| Class | Reference W | kWh per counter unit |
| --- | ---: | ---: |
| 0 | 1000 | 0.03333 |
| 1 | 1500 | 0.05 |
| 2 | 2000 | 0.06666 |
| 3 | 2500 | 0.08333 |
| 4 | 3000 | 0.1 |

The app's truncated factors are retained, without premature rounding. For a
synthetic example, class 2 and a raw daily counter of 30 yield 1.9998 kWh,
displayed as approximately 2.00 kWh. An unknown class yields no energy estimate.
The current class is used, as in the app; historical power settings cannot be
reconstructed if the device configuration was changed.

`GetTemperatureStatistic` returns `Date` and `T0` through `T23` directly in °C.
This differs from the tenths-of-a-degree current `T` field in `GetStatus`.
These are the convector's sensor readings, not a separate room sensor's history.

Neither graph proves instantaneous watts or whether the heating element is
currently active. A weekly programme or a setpoint difference is not treated
as a heating-state measurement.

## Polling and safety

A separate coordinator updates telemetry every 15 minutes, with its own session,
a 90-second overall limit and 15-second request limits. Initial telemetry runs
in the background after the platforms load. Climate polling continues on its
existing 30-second interval and its independent reconnecting transport.

The app requests seven records concurrently. Sequential requests can repeat
dates when status polling resets the device's read cursor. Graphs are fetched
in batches of at most seven concurrent requests and deduplicated by date.
An incomplete batch can expose fewer dates; absent dates are never invented.

Only `GetStatus`, `GetParams`, `GetPowerMode`, `GetEnergyStatistic` and
`GetTemperatureStatistic` are accepted by the telemetry reader. No power,
programme, tariff, setpoint or reset commands are added. Regulation is displayed
as a status, not a selectable watt limit. Credentials and pairing tokens are
not exposed in entity attributes or telemetry error messages.

The sensor becomes unavailable on a failed read or after 30 minutes without a
fresh snapshot. At midnight, yesterday's total does not become today's zero.
Removing the config entry cancels telemetry and closes its resources.

## Offline tests

Run in an environment containing Home Assistant and pyeldom 2.1.1:

```sh
python -B -m unittest discover -s tests -v
```

The fixtures are synthetic and make no calls to an Eldom device. They cover
conversion, unknown scales, zero versus missing data, invalid/future dates,
hour ordering, incomplete hours, DST date labels, freshness, read-only command
validation, background setup/unload, programme modes and transport recovery.

This integration supplies reusable sensor data. Custom household dashboards,
entity IDs, authentication material and deployment backups are outside its scope.
