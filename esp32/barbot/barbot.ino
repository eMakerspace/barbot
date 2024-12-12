
// Install stepper library:
// Sketch -> Include library -> Add .ZIP Library...
// Then, select AccelStepper-1.64.zip (in esp32 folder)


#include "AccelStepper.h"

int P[13]; // Array with 13 elements (P0 to P12)

// Define pins
#define OUTPUT_STEP 5
#define OUTPUT_DIR 18
#define OUTPUT_SLEEP 26
#define OUTPUT_RELAIS 2 //33  
#define OUTPUT_ENABLERELAIS 32 //32

#define INPUT_ENDSWITCH 4


#define DOWN1_TIME 800
#define DOWN2_TIME 400
#define DOWN3_TIME 2000
#define RELAISSWITCHING_TIME 50
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

int endswitchState = HIGH;
int old_endswitchState = HIGH;


void startP()
{
  currentState = MOVING_TO_POS;
  Serial.println("Moving to position: " + String(targetPositionIndex));
  stepper.moveTo(P[targetPositionIndex]);
}

void startZ()
{
  currentState = PRESSING;
  lastActionTime = millis();
  Serial.println("press for: " + String(pressDuration) + " ms");
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
  currentState = HOMING;
  stepper.setSpeed(SPEED_HOMING);
  Serial.println("Homing...");
}

// Function to handle M0 command (stop)
void handleM0() {
  currentState = STOPPED;
  Serial.println("Stop command received.");
  while(true);
}

void checkSerial()
{
  if (Serial.available() > 0) {
    char incomingChar = Serial.read();
    
    if (incomingChar == '\n') {
      if (commandBuffer != "M0")
      {
        commandQueue += commandBuffer + incomingChar;
        commandBuffer = "";
      }
      else
      {
        handleM0();
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
      } else if (currentCommand.startsWith("G28")) {
        handleG28();
      }
      break;

    case MOVING_TO_POS:
      if (stepper.distanceToGo() == 0) {
        Serial.println("Position set to: " + String(targetPositionIndex));
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
      if (millis() - lastActionTime - RELAISSWITCHING_TIME >= DOWN1_TIME) {
        enableRelais = false;
      }
      if (millis() - lastActionTime >= DOWN1_TIME) {
        lastActionTime = millis();
        currentState = DOWN2;
      }
      break;

    case DOWN2:
      enableRelais = true; // Hoch
      pressRingState = true;
      if (millis() - lastActionTime - RELAISSWITCHING_TIME >= DOWN2_TIME) {
        enableRelais = false;
       }
      if (millis() - lastActionTime >= DOWN2_TIME) {
          lastActionTime = millis();
          currentState = DOWN3;
       }
      break;

    case DOWN3:
      pressRingState = false; // Runter
      enableRelais = true;
      if (millis() - lastActionTime >= DOWN3_TIME) {
        enableRelais = false;
        setIdle();
      }
      break;


    case PRESSING:
      pressRingState = true; // HOCH
      enableRelais = true;
      if (millis() - lastActionTime >= pressDuration) {
        Serial.println("press finished");
        currentState = DOWN1;
        lastActionTime = millis();
      }
      break;

    case HOMING:
      stepper.runSpeed();
      if ((millis() - lastDebounceTime) > debounceDelay) {
        // If the switch is pressed (LOW state), stop the motor
        if (endswitchState == LOW) {
          stepper.stop();
          Serial.println("Homed successfully.");
          currentCommand = "";
          currentPositionIndex = 0;
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