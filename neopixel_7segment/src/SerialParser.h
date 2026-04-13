#pragma once
#include <cstdint>
#include <cstring>

// Command types parsed from serial input
enum class CommandType : uint8_t {
    Static,      // static display of a number: "42"
    Blink,       // blinking number: "-bl 44"
    Breathe,     // breathing number: "-br 82"
    Animate,     // fancy animations: "-i"
    Cup,         // falling cup animation: "-c"
    Invalid      // unrecognized command
};

// Parsed command structure
struct Command {
    CommandType type;
    uint8_t     value;   // the number to display (for Static/Blink/Breathe)

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
