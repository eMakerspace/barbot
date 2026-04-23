#pragma once
#include <cstdint>
#include <cstring>

// Command types parsed from serial input
enum class CommandType : uint8_t {
    Static,        // static display of a number: "42"
    Blink,         // blinking number: "-bl 44"
    Breathe,       // breathing number: "-br 82"
    Animate,       // fancy animations: "-i"
    Cup,           // falling cup animation: "-c"
    Brightness,    // set LED strip brightness: "-bri 128"
    Strobe,        // fast red strobe all LEDs: "-s"
    Count,         // count 00→99→00 at 0.4s takt: "-cnt"
    Moving,        // moving to slot animation: "-mv"
    Pouring,       // pouring spirit animation: "-pour"
    Mixing,        // mixing/dispensing animation: "-mix"
    Done,          // drink done celebration: "-done"
    CupWait,       // waiting for cup: "-cupwait"
    DrinkReady,    // drink ready: green ring pulse + attention bar/U: "-drinkready"
    OverdueStrobe, // overdue: full red strobe everything: "-overduestrobe"
    DrinkReadyNum, // escalating blink of order number: "-drinknum 42"
    Working,       // work-in-progress animation: "-w"
    EStop,         // emergency stop: full red strobe + "E" on 7-seg: "-estop"
    ErrorEffect,   // set LED fault/status effect: "-e RED_SOLID"
    FirmwareInfo,  // report firmware identity: "M115"
    Invalid        // unrecognized command
};

enum class LedEffectCode : uint8_t {
    Idle = 0,
    GreenSolid,
    RedSolid,
    RedFlashFast,
    RedPulse,
    YellowSolid,
    OrangeFlash,
    GreenFlashSlow,
    GreenFlashFast,
};

// Parsed command structure
struct Command {
    CommandType type;
    uint8_t     value;   // number or LedEffectCode (for ErrorEffect)

    Command() : type(CommandType::Invalid), value(0) {}
};

// SerialParser: stateful line reader with buffering.
// Accumulates incoming serial data until a complete line (\n or \r) is received.
class SerialParser {
public:
    // Check if a complete line is available and parse it into a Command.
    // Returns true if a valid command was parsed; cmd is filled with the result.
    static bool tryParse(Command &cmd);

private:
    // Parse a null-terminated command string into a Command struct
    static Command parseCommand(const char *line);
};
