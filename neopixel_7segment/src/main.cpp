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
    Serial.println("  <number>      - display 0-99 (static)");
    Serial.println("  -bl <num>     - blink number on 7-segment");
    Serial.println("  -br <num>     - breathing effect on 7-segment");
    Serial.println("  -i            - idle animations (~6 min 45 s cycle)");
    Serial.println("  -c            - falling cup animation (7-segment + vibrant ring flash)");
    Serial.println("  -s            - fast red strobe (all LEDs)");
    Serial.println("  -mv           - moving to slot animation (NeoPixel only)");
    Serial.println("  -pour         - pouring spirit animation (NeoPixel only)");
    Serial.println("  -mix          - mixing/dispensing animation (NeoPixel only)");
    Serial.println("  -done         - drink done celebration (NeoPixel only)");
    Serial.println("  -e <effect>   - set fault/status effect (RED_SOLID, RED_FLASH_FAST, RED_PULSE, YELLOW_SOLID, ORANGE_FLASH, GREEN_SOLID, GREEN_FLASH_SLOW, GREEN_FLASH_FAST, IDLE)");
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
                ledStrip.setCup();
                break;
            case CommandType::Strobe:
                Serial.println("Strobing red!");
                ledStrip.setStrobe();
                break;
            case CommandType::Moving:
                Serial.println("Moving...");
                ledStrip.setMoving();
                break;
            case CommandType::Pouring:
                Serial.println("Pouring...");
                ledStrip.setPouring();
                break;
            case CommandType::Mixing:
                Serial.println("Mixing...");
                ledStrip.setMixing();
                break;
            case CommandType::Done:
                Serial.println("Done!");
                ledStrip.setDone();
                break;
            case CommandType::DrinkReady:
                Serial.println("Drink ready!");
                ledStrip.setDrinkReady();
                break;
            case CommandType::OverdueStrobe:
                Serial.println("Overdue strobe!");
                ledStrip.setOverdueStrobe();
                break;
            case CommandType::DrinkReadyNum:
                Serial.print("Drink ready num: ");
                Serial.println(cmd.value);
                controller.setDrinkReady(cmd.value);
                break;
            case CommandType::ErrorEffect: {
                LedEffectCode effect = static_cast<LedEffectCode>(cmd.value);
                switch (effect) {
                    case LedEffectCode::Idle:
                        Serial.println("Effect: IDLE");
                        ledStrip.setIdle();
                        break;
                    case LedEffectCode::GreenSolid:
                        Serial.println("Effect: GREEN_SOLID");
                        ledStrip.setSolid(CRGB::Green);
                        break;
                    case LedEffectCode::RedSolid:
                        Serial.println("Effect: RED_SOLID");
                        ledStrip.setSolid(CRGB::Red);
                        break;
                    case LedEffectCode::RedFlashFast:
                        Serial.println("Effect: RED_FLASH_FAST");
                        ledStrip.setFlash(CRGB::Red, 90, 90);
                        break;
                    case LedEffectCode::RedPulse:
                        Serial.println("Effect: RED_PULSE");
                        ledStrip.setPulse(CRGB::Red, 1200);
                        break;
                    case LedEffectCode::YellowSolid:
                        Serial.println("Effect: YELLOW_SOLID");
                        ledStrip.setSolid(CRGB::Yellow);
                        break;
                    case LedEffectCode::OrangeFlash:
                        Serial.println("Effect: ORANGE_FLASH");
                        ledStrip.setFlash(CRGB(255, 120, 0), 140, 140);
                        break;
                    case LedEffectCode::GreenFlashSlow:
                        Serial.println("Effect: GREEN_FLASH_SLOW");
                        ledStrip.setFlash(CRGB::Green, 500, 500);
                        break;
                    case LedEffectCode::GreenFlashFast:
                        Serial.println("Effect: GREEN_FLASH_FAST");
                        ledStrip.setFlash(CRGB::Green, 100, 100);
                        break;
                }
                break;
            }
            case CommandType::Invalid:
                Serial.println("ERR: unknown command. Try: <num>, -bl <num>, -br <num>, -i, -c, -s, -mv, -pour, -mix, -done, -cupwait, -e <effect>");
                break;
        }
    }

    // Drive animation state machines
    controller.tick();
    ledStrip.tick();
}
