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

    if (strcmp(line, "-cnt") == 0) {
        cmd.type = CommandType::Count;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-s") == 0) {
        cmd.type = CommandType::Strobe;
        cmd.value = 0;
        return cmd;
    }

    if (strncmp(line, "-bri", 4) == 0) {
        cmd.type = CommandType::Brightness;
        int val = atoi(line + 4);
        cmd.value = (val >= 0 && val <= 255) ? (uint8_t)val : 200;
        return cmd;
    }

    if (strcmp(line, "-mv") == 0) {
        cmd.type = CommandType::Moving;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-pour") == 0) {
        cmd.type = CommandType::Pouring;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-mix") == 0) {
        cmd.type = CommandType::Mixing;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-done") == 0) {
        cmd.type = CommandType::Done;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-w") == 0) {
        cmd.type = CommandType::Working;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-estop") == 0) {
        cmd.type = CommandType::EStop;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-cupwait") == 0) {
        cmd.type = CommandType::CupWait;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-drinkready") == 0) {
        cmd.type = CommandType::DrinkReady;
        cmd.value = 0;
        return cmd;
    }

    if (strcmp(line, "-overduestrobe") == 0) {
        cmd.type = CommandType::OverdueStrobe;
        cmd.value = 0;
        return cmd;
    }

    if (strncmp(line, "-drinknum", 9) == 0) {
        cmd.type = CommandType::DrinkReadyNum;
        int val = atoi(line + 9);
        cmd.value = (val >= 0 && val <= 99) ? (uint8_t)val : 0;
        return cmd;
    }

    if (strncmp(line, "-e", 2) == 0) {
        const char *effect = line + 2;
        while (*effect == ' ' || *effect == '\t') effect++;

        cmd.type = CommandType::ErrorEffect;
        if (strcmp(effect, "IDLE") == 0) {
            cmd.value = (uint8_t)LedEffectCode::Idle;
            return cmd;
        }
        if (strcmp(effect, "GREEN_SOLID") == 0) {
            cmd.value = (uint8_t)LedEffectCode::GreenSolid;
            return cmd;
        }
        if (strcmp(effect, "RED_SOLID") == 0) {
            cmd.value = (uint8_t)LedEffectCode::RedSolid;
            return cmd;
        }
        if (strcmp(effect, "RED_FLASH_FAST") == 0) {
            cmd.value = (uint8_t)LedEffectCode::RedFlashFast;
            return cmd;
        }
        if (strcmp(effect, "RED_PULSE") == 0) {
            cmd.value = (uint8_t)LedEffectCode::RedPulse;
            return cmd;
        }
        if (strcmp(effect, "YELLOW_SOLID") == 0) {
            cmd.value = (uint8_t)LedEffectCode::YellowSolid;
            return cmd;
        }
        if (strcmp(effect, "ORANGE_FLASH") == 0) {
            cmd.value = (uint8_t)LedEffectCode::OrangeFlash;
            return cmd;
        }
        if (strcmp(effect, "GREEN_FLASH_SLOW") == 0) {
            cmd.value = (uint8_t)LedEffectCode::GreenFlashSlow;
            return cmd;
        }
        if (strcmp(effect, "GREEN_FLASH_FAST") == 0) {
            cmd.value = (uint8_t)LedEffectCode::GreenFlashFast;
            return cmd;
        }
        cmd.type = CommandType::Invalid;
        return cmd;
    }

    if (strcmp(line, "M115") == 0) {
        cmd.type = CommandType::FirmwareInfo;
        cmd.value = 0;
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
