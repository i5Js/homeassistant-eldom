# IoT convector configuration controls

Seven configuration controls extend the existing IoT convector device:

| Domain | Control | Values |
| --- | --- | --- |
| Switch | Child lock | On / off |
| Switch | Antifreeze protection | On / off |
| Switch | Power regulation | On / off; this does not select a watt limit |
| Switch | Automatic display dimming | On / off |
| Switch | Sleep mode | On / off, in Manual mode |
| Select | Window detection | Off / on / smart |
| Number | Display brightness | Integer levels 1–10; not a percentage |

These are configuration entities, with separate stable unique IDs. Existing
climate, energy and read-only entities keep their identities and availability
rules. Names and enum values have English and Spanish translations.

## Writes and confirmation

Each operation reads the current configuration, sends at most one write, then
reads it again. The acknowledgement must identify the command with `Code=0`
and `Type=OK`; the independent reading must also match the requested value.
An already confirmed value needs no write. Brightness and automatic dimming
share one vendor command, so both fields are read and the other is preserved.

The operation uses the same lock as climate and history requests. Its timeout
is 30 seconds after acquiring that lock. It can first wait for an ongoing
history traversal. Writes are never automatically retried, including after
authentication errors, timeouts or ambiguous acknowledgements. A rejected
token is discarded for the next independent request.

An uncertain write makes the affected configuration unavailable until the
next auxiliary reading; no requested value is published optimistically.
Only independent confirmed readings update the shared feature snapshot. They
do not change its retrieval timestamp, postpone the 15-minute history refresh
or turn an earlier failed refresh into a successful one.

Sleep checks a fresh `GetStatus` before writing and requires Manual mode. It
does not switch a weekly programme to Manual or rewrite the saved schedule.
A request in another mode fails with an explanatory message and sends no
write. The user chooses the operating mode separately.

The climate temperature control also reads its live `GetParams` before
`SetParams`, preserving clock synchronization, tariff times, lock and the entire
`SystemSettings` string. Its write uses whole degrees, while the independent
reading returns tenths. The existing whole-degree conversion is retained.
Missing parameters prevent the write, and confirmation checks all the other
returned settings for unintended changes. This replaces the library's fixed
parameter values, which could overwrite configuration when adjusting temperature.

## Validation and limits

Command fields were inspected in the Android app 3.4.0. Each control was then
changed and restored individually through the cloud on firmware 2.3.0 /
hardware 1.1.0. Both directions were confirmed by separate device readings;
all three window options were exercised. Brightness was checked at levels
5 and 6, with the app supplying the supported 1–10 range. Sleep was checked
in Manual mode and switched off again. This verifies configuration commands,
not the thermal behaviour of antifreeze, window detection or an overnight
sleep temperature curve.

Offline regressions also cover malformed values, coupled fields, rejected
acknowledgements, mismatched readback, uncertain network writes, cancellation,
authentication, lock contention and preservation of history freshness. No
Wi-Fi, AP, pairing, firmware update, counter reset, schedule editing or clock
write is exposed by these controls.
