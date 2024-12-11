void setup() {
  // Start serial communication at 9600 baud
  Serial.begin(9600);
}

void loop() {
  // Check if data is available to read
  if (Serial.available() > 0) {
    // Create a buffer to hold the incoming string
    String incomingString = Serial.readString();
    
    // Print the received string to the Serial Monitor
    Serial.print("Received: ");
    Serial.println(incomingString);

    // Send a response back to the Serial Monitor
    Serial.print("Sending back: ");
    Serial.println(incomingString);
  }
}
