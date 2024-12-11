#define GPIO 2

enum State {
  IDLE,          // Waiting for new commands
  MOVING_TO_POS,    // Moving to a position (X)
  PRESSING,         // Pressing for a duration (Z)
  HOMING,           // Homing (G28)
  STOPPED           // Stop command (M0)
};

State currentState = IDLE;
String commandQueue = "";
String currentCommand = "";  // The G-code string being processed
String nextCommand = "";     // To accumulate subsequent commands
String commandBuffer = "";
unsigned long lastActionTime = 0;  // Time when the current action started
unsigned long pressDuration = 0;   // Duration for the press
int targetPosition = 0;          // Target position (1 to 12)
int currentPosition = 0;
int pressRingState = LOW;           // Speaker state (on/off)
bool combinedCommand = false;


void startP()
{
  currentState = MOVING_TO_POS;
  Serial.print("Moving to position: " + String(targetPosition));
  Serial.println(" (Difference: " +  String(targetPosition - currentPosition) + ")");
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

// Function to handle G1 command
void handleG1(String command) {
  int xPos = -1;
  int zDuration = -1;
  
  // Parse the command for X and Z
  int xIndex = command.indexOf('X');
  if (xIndex != -1) {
    xPos = command.substring(xIndex + 1).toInt();
  }
  
  int zIndex = command.indexOf('Z');
  if (zIndex != -1) {
    zDuration = command.substring(zIndex + 1).toInt();
  }

  if (xPos != -1) {
    targetPosition = xPos;
    startP();

    if (zDuration != -1) {
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
    Serial.println("Invalid command");
  }
}

// Function to handle G28 command (home)
void handleG28() {
  currentState = HOMING;
  Serial.println("Homing...");
}

// Function to handle M0 command (stop)
void handleM0() {
  currentState = STOPPED;
  Serial.println("Stop command received.");
  while(true);
}

int countCommands(String str) {
  int count = 0;
  for (int i = 0; i < str.length(); i++) {
    if (str.charAt(i) == '\n') {
      count++;
    }
  }
  return count;
}

void setup() {
  Serial.begin(9600);
  Serial.println("INIT");
  pinMode(GPIO, OUTPUT);

}

void loop() {
  // Check if a new command is received
  if (Serial.available() > 0) {
    char incomingChar = Serial.read();
    
    if (incomingChar == '\n') {
      if (commandBuffer != "M0")
      {
        commandQueue += commandBuffer + incomingChar;
        Serial.println("*************************************************3");
        Serial.println("Received: " + commandBuffer + "             commands in queue: " + String(countCommands(commandQueue)));
        Serial.println("Queue:" + commandQueue);
        Serial.println("*************************************************3");
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
      // Simulate moving to the target position
      Serial.println("Position set to: " + String(targetPosition));
      currentPosition = targetPosition;
      if (combinedCommand)
      {
        combinedCommand = false;
        startZ();
      }
      else
      {
        setIdle();
      }
      break;

    case PRESSING:
      // Turn on the speaker for the press duration
      if (millis() - lastActionTime >= pressDuration) {
        Serial.println("press finished");
        pressRingState = LOW;
        setIdle();
      } else {
        if (pressRingState == LOW) {
          pressRingState = HIGH;
        }
      }
      break;

    case HOMING:
      // Simulate homing (e.g., moving to position 0 or a predefined home position)
      Serial.println("Homed successfully.");
      currentCommand = "";
      currentPosition = 0;
      currentState = IDLE;  // Transition back to waiting after homing
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

  digitalWrite(GPIO, pressRingState);
}