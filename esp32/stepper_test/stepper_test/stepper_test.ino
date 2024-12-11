// Install stepper library:
// Sketch -> Include library -> Add .ZIP Library...
// Then, select AccelStepper-1.64.zip (in esp32 folder)


#include "AccelStepper.h"

// Define pins
#define OUTPUT_STEP 5
#define OUTPUT_DIR 18
#define OUTPUT_SLEEP 26
#define INPUT_ENDSWITCH 4


AccelStepper stepper(AccelStepper::DRIVER, OUTPUT_STEP, OUTPUT_DIR);

void setup() {
  pinMode(OUTPUT_SLEEP, OUTPUT);
  digitalWrite(OUTPUT_SLEEP, HIGH);
  pinMode(INPUT_ENDSWITCH, INPUT);
  digitalWrite(INPUT_ENDSWITCH, LOW);

  stepper.setMaxSpeed(1000);   // Max speed (steps per second)
  stepper.setAcceleration(500); // Acceleration (steps per second^2)

  Serial.begin(115200);
  Serial.println("Init done");
}

void loop() {
  stepper.moveTo(200);
  stepper.runToPosition();


  digitalWrite(OUTPUT_SLEEP, LOW);
  delay(1000); 

  digitalWrite(OUTPUT_SLEEP, HIGH);
  stepper.moveTo(-200);
  stepper.runToPosition();

  
  digitalWrite(OUTPUT_SLEEP, LOW);
  delay(1000);
}
