#include <Arduino.h>
#include "config.h"
#include "SevenSegmentDriver.h"
#include "DisplayController.h"
#include "SerialParser.h"
#include "LedStripController.h"

// ---------------------------------------------------------------------------
// Static instances — no heap allocation
// ---------------------------------------------------------------------------
static SevenSegmentDriver   display;
static DisplayController    controller(display);
static LedStripController   ledStrip;

// ---------------------------------------------------------------------------
// Display task — runs on core 0, isolated from all Arduino/system activity
// ---------------------------------------------------------------------------
static void displayTask(void *arg) {
    // The refresh loop busy-waits with delayMicroseconds and never yields.
    // Without this call, the FreeRTOS IDLE0 task on core 0 is starved,
    // triggering the task watchdog and resetting the ESP32 after ~5 s.
    // disableCore0WDT() removes IDLE0 from watchdog supervision.
    disableCore0WDT();
    static_cast<SevenSegmentDriver*>(arg)->runLoop();
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);

    ledStrip.begin();
    ledStrip.setIdle();

    display.begin();
    display.setOnes(0);
    display.setTens(0);

    // Pin display task to core 0 at priority 2 (above loop's priority 1)
    // so it never contends with application or system tasks on core 1.
    xTaskCreatePinnedToCore(
        displayTask,   // task function
        "display",     // name (visible in WDT error messages)
        4096,          // stack — sized for nested register-level calls
        &display,      // argument passed to task function
        2,             // priority
        nullptr,       // task handle (not needed)
        0              // core 0
    );

    Serial.println("7-segment display ready.");
    Serial.println("Commands:");
    Serial.println("  <number>   - display 0-99 (static)");
    Serial.println("  -bl <num>  - blink number");
    Serial.println("  -br <num>  - breathing effect");
    Serial.println("  -i         - idle animations (~6 min 45 s cycle)");
    Serial.println("  -c         - falling cup animation");
}

// ---------------------------------------------------------------------------
// Loop (core 1) — serial parser and controller, never touches hardware directly
// ---------------------------------------------------------------------------
void loop() {
    // Check for incoming serial commands
    Command cmd;
    if (SerialParser::tryParse(cmd)) {
        switch (cmd.type) {
            case CommandType::Static:
                Serial.print("Display: ");
                Serial.println(cmd.value);
                controller.setStatic(cmd.value);
                break;
            case CommandType::Blink:
                Serial.print("Blinking: ");
                Serial.println(cmd.value);
                controller.setBlinking(cmd.value);
                break;
            case CommandType::Breathe:
                Serial.print("Breathing: ");
                Serial.println(cmd.value);
                controller.setBreathing(cmd.value);
                break;
            case CommandType::Animate:
                Serial.println("Idle animations...");
                controller.setAnimating();
                ledStrip.setIdle();
                break;
            case CommandType::Cup:
                Serial.println("Cup animation...");
                controller.setCup();
                break;
            case CommandType::Invalid:
                Serial.println("Invalid command");
                break;
        }
    }

    // Drive animation state machines
    controller.tick();
    ledStrip.tick();
}
