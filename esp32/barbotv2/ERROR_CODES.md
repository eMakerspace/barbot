# BarBot ESP32 Error Codes & States

## Sensor/Hardware Failures

1. **HX711_NOT_DETECTED** - Scale chip not responding at startup
2. **HX711_READ_ERROR** - Scale communication failure during operation (multiple consecutive read failures)
3. **SCALE_NOT_CALIBRATED** - Scale calibration factor is invalid (< 1.0 counts/gram)
4. **CUP_MISSING** - Light curtain detects no cup present at start of operation
5. **CUP_REMOVED** - Cup removed mid-operation (light curtain suddenly blocked)
6. **STEPPER_END_SWITCH_FAILED** - End switch not triggered during homing sequence
7. **STEPPER_TIMEOUT** - Stepper movement did not complete within expected time

## Fill/Dispensing Errors

8. **PUMP_FAILURE** - Pump not dispensing (insufficient weight change: < 25g over 15 samples)
9. **PUMP_BLOCKED** - Tubing blocked or pump stuck (no weight change for 25 consecutive samples)
10. **EMPTY_BOTTLE** - Weight not changing during fill operation (bottle empty or no liquid)
11. **FILL_TIMEOUT** - Fill operation exceeded 30 second timeout
12. **WEIGHT_OUTLIER** - Scale reading is invalid/out of range (spike/spike/noise detected)

## Motion/Safety Errors

13. **SERVO_FORBIDDEN_ZONE** - Servo movement attempted in forbidden zone at unsafe angle (< 120°)
14. **SERVO_NOT_AT_SAFE** - Stepper movement attempted without servo at 180° (auto-corrected)
15. **STEPPER_OUT_OF_BOUNDS** - Stepper position exceeds maximum steps (5958)
16. **SERVO_ANGLE_INVALID** - Servo command sent with invalid angle (< 0° or > 180°)
17. **STEPPER_POSITION_MISMATCH** - Stepper position does not match expected value

## Communication/Protocol Errors

18. **SERIAL_BUFFER_OVERFLOW** - Command buffer full, cannot accept more commands
19. **INVALID_GCODE_FORMAT** - G-code command format is malformed/unparseable
20. **INVALID_PARAMETER** - G-code parameter out of valid range
21. **UNKNOWN_COMMAND** - G-code command not recognized
22. **SERIAL_TIMEOUT** - No data received from Pi for extended period (watchdog)

## Pump/Command State Errors

23. **PUMP_ALREADY_RUNNING** - Fill command received while pump already dispensing
24. **INVALID_PUMP_INDEX** - Pump index does not exist (not 0-3)
25. **CONFLICTING_COMMAND** - Command conflicts with current system state

## Servo Calibration/Configuration

26. **FORBIDDEN_ZONE_NOT_SET** - Slot 4/5 positions not received from Pi (G5 command)
27. **SERVO_ZONE_SAFETY_BLOCK** - Multiple servo attempts blocked in forbidden zone

## System State Errors

28. **EMERGENCY_STOP** - Emergency stop command received
29. **EMERGENCY_STOP_TIMEOUT** - Extended time in emergency stop state
30. **MOTION_DURING_EMERGENCY** - Movement attempt while emergency stop active

## Power/System Health

31. **LOW_POWER** - Supply voltage below safe operating threshold (if monitored)
32. **WATCHDOG_TIMEOUT** - System watchdog timeout triggered
33. **MEMORY_CRITICAL** - Heap/memory usage critical
34. **TEMPERATURE_HIGH** - ESP internal temperature exceeds safe range

## Communication Status (Non-Errors)

35. **SERIAL_CONNECTED** - Serial connection established
36. **SERIAL_DISCONNECTED** - Serial connection lost
37. **SYSTEM_READY** - All diagnostics passed, system operational
38. **SCALE_READY** - Scale initialized and responding

---

## Error Severity Levels

**CRITICAL (Stop Everything):**
- HX711_NOT_DETECTED, CUP_MISSING, STEPPER_END_SWITCH_FAILED, EMERGENCY_STOP, LOW_POWER, WATCHDOG_TIMEOUT

**HIGH (Stop Current Operation):**
- PUMP_FAILURE, PUMP_BLOCKED, FILL_TIMEOUT, SERVO_FORBIDDEN_ZONE, STEPPER_OUT_OF_BOUNDS, HX711_READ_ERROR

**MEDIUM (Warn, Allow Retry):**
- WEIGHT_OUTLIER, SERVO_NOT_AT_SAFE, STEPPER_POSITION_MISMATCH, INVALID_PARAMETER, CUP_REMOVED

**LOW (Informational):**
- SERIAL_CONNECTED, SYSTEM_READY, SCALE_READY, SERVO_ZONE_SAFETY_BLOCK
