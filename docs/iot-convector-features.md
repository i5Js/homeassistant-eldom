# IoT convector configuration readings

This extension adds read-only configuration entities for IoT `HTRCNV`
convectors. Requests and field meanings were checked against the official app
and responses from firmware 2.3.0 / hardware 1.1.0, through both its AP and cloud
API. The integration continues to use the cloud; this does not introduce LAN
or AP support into Home Assistant.

## Entities

All entities share the existing heater device. Existing climate and estimated
energy unique IDs are unchanged. Names are provided in English and Spanish.

| Entity | Value | Default |
| --- | --- | --- |
| Child lock enabled | Explicit enabled/disabled flag | Enabled |
| Antifreeze protection enabled | Protection configuration, not current heating | Enabled |
| Sleep mode enabled | Explicit mode flag | Enabled |
| Power regulation enabled | Regulation configuration, not an instantaneous watt limit | Enabled |
| Open window detection mode | `off`, `on`, `smart`; not a live window sensor | Enabled |
| Weekly schedule enabled intervals | Enabled interval count and full schedule attributes | Enabled |
| Automatic display dimming enabled | Explicit adaptive display flag | Disabled diagnostic |
| Wi-Fi mode enabled | Device network mode flag | Disabled diagnostic |
| Access point mode enabled | Device AP mode flag | Disabled diagnostic |
| Automatic clock synchronization enabled | Clock configuration flag | Disabled diagnostic |
| Display brightness level | Original vendor level, not a percentage | Disabled diagnostic |
| Display language | Language code; supported codes in attributes | Disabled diagnostic |
| Firmware version | Device-reported version | Disabled diagnostic |
| Hardware version | Device-reported version | Disabled diagnostic |
| Day tariff start | Local `HH:MM`; not an electricity price | Disabled diagnostic |
| Night tariff start | Local `HH:MM`; not an electricity price | Disabled diagnostic |
| Scheduler configuration | `weekly_program` or `timer`; not the active HVAC mode | Disabled diagnostic |
| Energy estimate reference power | Reference used by the app calculation, not measured watts or a verified nominal rating | Disabled diagnostic |

The weekly schedule retains all seven slots, including disabled ones. Each has
`slot`, `enabled`, `days_mask`, `days`, `start`, `end`, and `temperature` in °C.
The day mask uses Sunday as bit 0 through Saturday as bit 6. The `complete`
attribute distinguishes incomplete enabled-slot details; missing enable flags
also make the count unavailable. Incomplete fields remain null.

## Availability and updates

Climate and auxiliary reads each own an asynchronous DNS channel and close it
with their HTTP session. Explicit resolver options avoid aiohttp's shared
resolver manager and the event loop's threaded DNS executor, so a stalled
lookup there does not prevent a new Eldom connection. System DNS servers are
retained; no vendor IP address or public DNS server is hardcoded.

Configuration and history share the auxiliary 15-minute background coordinator.
There is no separate request per entity. Configuration requests are individually
serialized with climate polls and existing controls. A history traversal holds
the same entry-owned gate for its entire contiguous request sequence so the
integration's other requests cannot reset the cursor halfway through.

The history gate is limited to 45 seconds per traversal. In normal conditions,
seven combined graphs complete sooner. The full background update is bounded
at 90 seconds, individual HTTP requests at 15 seconds and optional feature
queries at 5 seconds. An existing climate command can wait for a history
transaction to finish; commands retain their original protocol and are never
automatically replayed. A failed command does not publish an optimistic change.
Third-party clients, including the manufacturer's app, may still interrupt the
device's shared history cursor.

Fresh zero/false values remain valid. Missing fields, unknown enum values,
unsupported optional replies and data older than 30 minutes are unavailable.
An unsupported optional feature does not hide successful siblings. History-only
failure can leave fresh configuration available while energy becomes
unavailable; successfully obtained partial dates are retained. Authentication
failure and full update failure remain visible through coordinator availability.

Only reviewed `Get…` requests are accepted by the auxiliary reader. No
configuration switches, schedule editor, language selector, reset, firmware
update, discovery or pairing operation is introduced. Neither `Status`, the
HVAC mode nor a setpoint comparison is used to invent an active-heater entity.

These are experimental vendor protocol readings. Different devices/firmware may
omit fields; enabled default does not guarantee support on every convector.
The existing boiler platforms and the legacy API keep their behaviour.

See [hourly telemetry](iot-convector-telemetry.md) for calculation, coverage and
bounded history recovery. Tests use synthetic devices and local fixtures, not
accounts or a real radiator:

```sh
python -B -m unittest discover -s tests -v
```

The read-only extension was validated with 58 offline tests in the HA runtime.
