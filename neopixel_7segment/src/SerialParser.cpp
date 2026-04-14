#include "SerialParser.h"
#include <Arduino.h>
#include <cctype>

bool SerialParser::tryParse(Command &cmd) {
    if (!Serial.available()) return false;

    // readStringUntil blocks until newline or timeout.
    // Set a short timeout (50 ms) so we don't stall the loop.
    Serial.setTimeout(50);
    String line = Serial.readStringUntil('\n');
    line.trim();  // removes \r, spaces, etc.

    if (line.isEmpty()) return false;

    cmd = parseCommand(line.c_str());
    return true;  // always return true so the caller can handle Invalid (e.g. print an error)
}

Command SerialParser::parseCommand(const char *line) {
    Command cmd;

    // Skip leading whitespace
    while (*line && (*line == ' ' || *line == '\t')) {
        line++;
    }

    // Check for command prefixes
    if (strncmp(line, "-bl", 3) == 0) {
        cmd.type = CommandType::Blink;
        int val = atoi(line + 3);
        cmd.value = (val >= 0 && val <= 99) ? (uint8_t)val : 0;
        return cmd;
    }

    if (strncmp(line, "-br", 3) == 0) {
        cmd.type = CommandType::Breathe;
        int val = atoi(line + 3);
        cmd.value = (val >= 0 && val <= 99) ? (uint8_t)val : 0;
        return cmd;
    }

    if (strcmp(line, "-i") == 0) {
        cmd.type = CommandType::Animate;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-c") == 0) {
        cmd.type = CommandType::Cup;
        cmd.value = 0;
        return cmd;
    }

    if (strncmp(line, "-bri", 4) == 0) {
        cmd.type = CommandType::Brightness;
        int val = atoi(line + 4);
        cmd.value = (val >= 0 && val <= 255) ? (uint8_t)val : 200;
        return cmd;
    }

    // Try parsing as a plain number (0–99) for static display
    if (*line && isdigit(*line)) {
        int val = atoi(line);
        if (val >= 0 && val <= 99) {
            cmd.type = CommandType::Static;
            cmd.value = (uint8_t)val;
            return cmd;
        }
    }

    cmd.type = CommandType::Invalid;
    return cmd;
}
