
// Install stepper library:
// Sketch -> Include library -> Add .ZIP Library...
// Then, select AccelStepper-1.64.zip (in esp32 folder)


#include "AccelStepper.h"

int P[13]; // Array with 13 elements (P0 to P12)

// Define pins
#define OUTPUT_STEP 5
#define OUTPUT_DIR 18
#define OUTPUT_SLEEP 26
#define OUTPUT_RELAIS 33 //33  
#define OUTPUT_ENABLERELAIS 32 //32

#define INPUT_ENDSWITCH 4


#define DOWN1_TIME 800
#define DOWN2_TIME 500
#define DOWN3_TIME 2000
#define RELAISSWITCHING_TIME1 500
#define RELAISSWITCHING_TIME2 250
#define DEBOUNCE_TIME 20

#define SPEED_DEFAULT 6000
#define SPEED_HOMING 2000


enum State {
  IDLE,          // Waiting for new commands
  MOVING_TO_POS,    // Moving to a position (X)
  PRESSING,         // Pressing for a duration (Z)
  DOWN1,
  DOWN2,
  DOWN3,
  HOMING,           // Homing (G28)
  STOPPED           // Stop command (M0)
};

AccelStepper stepper(AccelStepper::DRIVER, OUTPUT_STEP, OUTPUT_DIR);
State currentState = IDLE;
String commandQueue = "";
String currentCommand = "";  // The G-code string being processed
String nextCommand = "";     // To accumulate subsequent commands
String commandBuffer = "";
unsigned long lastActionTime = 0;  // Time when the current action started
unsigned long lastDebounceTime = 0;
unsigned long pressDuration = 0;   // Duration for the press
int targetPositionIndex = 0;          // Target position (1 to 12)
int currentPositionIndex = 0;
bool pressRingState = false; 
bool enableRelais = false;
bool combinedCommand = false;
bool homing = false;

int endswitchState = HIGH;
int old_endswitchState = HIGH;


void startP()
{
  currentState = MOVING_TO_POS;
  stepper.moveTo(P[targetPositionIndex]);
}

void startZ()
{
  currentState = PRESSING;
  lastActionTime = millis();
}

void setIdle()
{
  currentState = IDLE;
  currentCommand = "";
}

void invalidCommand()
{
  Serial.println("Invalid command");
  setIdle();
}

// Function to handle G1 command
void handleG1(String command) {
  int xPos = -1;
  int zDuration = -1;
  
  // Parse the command for X and Z
  int xIndex = command.indexOf('X');
  if (xIndex != -1) {
    xPos = command.substring(xIndex + 1).toInt();
    if (xPos > 12 || xPos < 0)
    {
      invalidCommand();
      return;
    }
  }
  
  int zIndex = command.indexOf('Z');
  if (zIndex != -1) {
    zDuration = command.substring(zIndex + 1).toInt();
    if (zDuration < 0)
    {
      invalidCommand();
    }
  }

  if (xPos != -1) {
    targetPositionIndex = xPos;
    startP();

    if (zIndex != -1) {
      pressDuration = zDuration;
      combinedCommand = true;
    } 
  }

  if (zDuration != -1 && xPos == -1) {
    pressDuration = zDuration;
    startZ();
  }

  if (zDuration == -1 && xPos == -1)
  {
    invalidCommand();
  }
}

// Function to handle G28 command (home)
void handleG28() {
  homing = true;
  currentState = DOWN3;
  lastActionTime = millis();
}

// Function to handle M0 command (stop)
void handleM0() {
  currentState = STOPPED;
  Serial.println("STOP");
  while(true);
}

void checkSerial()
{
  if (Serial.available() > 0) {
    char incomingChar = Serial.read();
    
    if (incomingChar == '\n') {
      if (commandBuffer == "M0")
      {
        handleM0();
      }
      else if (commandBuffer == "STATUS")
      {
        if(currentCommand == "") Serial.println("idle");
        else Serial.println("busy");
        commandBuffer = "";
      }
      else
      {
        commandQueue += commandBuffer + incomingChar;
        commandBuffer = "";
        Serial.println("ACK");
      }
      
    } else {
      // Append to current command
      commandBuffer += incomingChar;
    }
  }
}

void setup() {
  Serial.begin(9600);
  Serial.println("INIT");

  pinMode(OUTPUT_SLEEP, OUTPUT);
  pinMode(INPUT_ENDSWITCH, INPUT);
  pinMode(OUTPUT_ENABLERELAIS, OUTPUT);
  pinMode(OUTPUT_RELAIS, OUTPUT);
  digitalWrite(OUTPUT_RELAIS, LOW);
  digitalWrite(OUTPUT_ENABLERELAIS, LOW);
  digitalWrite(OUTPUT_SLEEP, HIGH);
  digitalWrite(INPUT_ENDSWITCH, LOW);

  int microstep = 8;
  int offset1 = -115;
  int offset2 = -20;

  P[0] = 0;
  P[1] =  microstep * (-500  + offset1);
  P[2] =  microstep * (-1000 + offset1);
  P[3] =  microstep * (-1500 + offset1);
  P[4] =  microstep * (-2010 + offset1);
  P[5] =  microstep * (-2510 + offset1);
  P[6] =  microstep * (-3010 + offset1);
  P[7] =  microstep * (-3500 + offset1 + offset2);
  P[8] =  microstep * (-4000 + offset1 + offset2);
  P[9] =  microstep * (-4500 + offset1 + offset2);
  P[10] = microstep * (-5000 + offset1 + offset2);
  P[11] = microstep * (-5500 + offset1 + offset2);
  P[12] = microstep * (-6000 + offset1 + offset2);

  stepper.setMaxSpeed(6000);   // Max speed (steps per second)
  stepper.setAcceleration(3000); // Acceleration (steps per second^2)
}

void loop() {
  endswitchState = digitalRead(INPUT_ENDSWITCH);
  if (endswitchState != old_endswitchState)
  {
    lastDebounceTime = millis();
  }

  checkSerial();

  if (currentState != HOMING)
  {
    stepper.run();
  }

  // State machine processing
  switch (currentState) {
    case IDLE:
      // Idle state, waiting for new commands
      if (currentCommand.startsWith("G1")) {
        handleG1(currentCommand);
      } else if (currentCommand == "G28") {
        handleG28();
      }
      else if (currentCommand != "")
      {
        invalidCommand();
      }
      break;

    case MOVING_TO_POS:
      if (stepper.distanceToGo() == 0) {
        currentPositionIndex = targetPositionIndex;
        if (combinedCommand)
        {
          combinedCommand = false;
          startZ();
        }
        else
        {
          setIdle();
        }
      }
      break;
    
    case DOWN1:
      pressRingState = false; // Runter
      enableRelais = true;

      if (millis() - lastActionTime >= DOWN1_TIME) {
        enableRelais = false;
      }
      if (millis() - lastActionTime >= DOWN1_TIME + (RELAISSWITCHING_TIME1) / 2) {
        pressRingState = true;
      }
      if (millis() - lastActionTime >= DOWN1_TIME + RELAISSWITCHING_TIME1) {
        lastActionTime = millis();
        currentState = DOWN2;
      }
      
      break;

    case DOWN2:
      pressRingState = true; // Hoch
      enableRelais = true;

      if (millis() - lastActionTime >= DOWN2_TIME) {
        enableRelais = false;
      }
      if (millis() - lastActionTime >= DOWN2_TIME + (RELAISSWITCHING_TIME2) / 2) {
        pressRingState = false;
      }
      if (millis() - lastActionTime >= DOWN2_TIME + RELAISSWITCHING_TIME2) {
        lastActionTime = millis();
        currentState = DOWN3;
      }

      break;

    case DOWN3:
      pressRingState = false; // Runter
      enableRelais = true;
      if (millis() - lastActionTime >= DOWN3_TIME) {
        enableRelais = false;

        if(homing)
        {
          stepper.setSpeed(SPEED_HOMING);
          currentState = HOMING;
        }
        else
        {
          setIdle();
        }
      }
      break;

    case PRESSING:
      pressRingState = true; // HOCH
      enableRelais = true;
      if (millis() - lastActionTime >= pressDuration) {
        currentState = DOWN1;
        lastActionTime = millis();
      }
      break;

    case HOMING:
      stepper.runSpeed();
      if ((millis() - lastDebounceTime) > DEBOUNCE_TIME) {
        // If the switch is pressed (LOW state), stop the motor
        if (endswitchState == HIGH) {
          stepper.stop();
          currentCommand = "";
          currentPositionIndex = 0;
          homing = false;
          stepper.setCurrentPosition(0);
          stepper.setSpeed(SPEED_DEFAULT);
          currentState = IDLE;  // Transition back to waiting after homing
        }
      }
      break;

    case STOPPED:
      // Handle the stop command: turn off speaker if it's on
      currentCommand = "";
      pressRingState = LOW;
      break;

    default:
      currentState = IDLE;  // Default state transition in case of error
      break;
  }

  if(commandQueue.length() > 0 && currentCommand == "") {
    // Find the position of the first '\n'
    int newlineIndex = commandQueue.indexOf('\n');
    // If there's a newline character, extract the first command
    if (newlineIndex != -1) {
      // Extract the substring up to the newline character (excluding '\n')
      currentCommand = commandQueue.substring(0, newlineIndex);

      // Remove the first command and the '\n' from the commandQueue
      commandQueue.remove(0, newlineIndex + 1); 
    }
  }

  digitalWrite(OUTPUT_RELAIS, pressRingState);
  digitalWrite(OUTPUT_ENABLERELAIS, enableRelais);
  old_endswitchState = endswitchState;
}