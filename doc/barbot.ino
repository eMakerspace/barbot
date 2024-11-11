/*
 *          BARBOT Arduino Code
 *          V1.0
 *          
 *          Author:   Kai Erdin
 *          Date:     03.07.2020
 *          
 */

#include <BluetoothSerial.h>

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth is not enabled! Please run `make menuconfig` to and enable it
#endif

#define DELAY_BETWEEN_INGREDIENTS 1000

#define PARK_SPEED 150
#define HOME_SPEED 600
#define MAX_SPEED 1500
#define ACCELERATION 20
#define MAX_POS -6564

#define OUTPUT_RELAIS 33  
#define OUTPUT_ENABLERELAIS 32    //ANPASSEN!!!!!!!!!!!!!
#define OUTPUT_STEP 5
#define OUTPUT_DIR 18
#define OUTPUT_SLEEP 26

#define ACCELERATIONSTEP 2400
#define ACCELERATIONDELAY 110
#define TOPSPEEDDELAY 10

#define INPUT_ENDSWITCH 4

BluetoothSerial SerialBT;

// BLUETOOTH ZEUGS
String serialBuffer = ""; // Bluetooth input
String actions[30];       // Schlussendliches Rezept
char ch = 0;              // Gelesener Character im bluetooth input
int actionIndex = 0;      // Für die Bearbeitung vom Bluetooth input
int lastIndex = 0;        // Für die Bearbeitung vom Bluetooth input


// STEPPER ZEUGS
bool dir = false;         // Nach links: False, nach rechts: true
bool stp = false;         // Nach einer flanke dreht der motor um 1 step.

int accelcounter = 0;     // Delay für die Beschleunigung / Verzögerung
int drivecounter = 0;     // Delay für das normale Fahren
int stepcounter = 0;      // Schrittzähler
int totalSteps = 0;       // Totale steps (4800 steps für beschl / verzögerung inbegriffen)

const int accelerationParameter = ACCELERATIONSTEP * ACCELERATIONSTEP / 100; // Konstante für die Beschleunigung

enum state {accelerate, moving, decelerate, homeAxis} state = accelerate;

void setup() 
{
  pinMode(OUTPUT_STEP, OUTPUT);
  pinMode(OUTPUT_DIR, OUTPUT);

  pinMode(OUTPUT_RELAIS, OUTPUT);
  pinMode(OUTPUT_ENABLERELAIS, OUTPUT);

  pinMode(OUTPUT_SLEEP, OUTPUT);
  digitalWrite(OUTPUT_SLEEP, HIGH);

  pinMode(INPUT_ENDSWITCH, INPUT);
  digitalWrite(INPUT_ENDSWITCH, LOW);
  
  SerialBT.begin("Barbot_testtest"); //Bluetooth device name
  
  for(int y=0; y<30; y++) 
  { //set all Values to 0
    actions[y] = "0";
  }
  
  
  

  
  
  
  
  digitalWrite(OUTPUT_ENABLERELAIS, LOW); //Disable Power
  delay(50);  //50ms switching time
  digitalWrite(OUTPUT_RELAIS, LOW); //Ring DOWN
  delay(50);
  digitalWrite(OUTPUT_ENABLERELAIS, HIGH); //Enable Power
  delay(1300);
  digitalWrite(OUTPUT_ENABLERELAIS, LOW); //Disable Power after Ring moved down for 600ms

  homeXAxis();

  DisableStepper();
}

void loop() 
{
  
  if (SerialBT.available()) 
  {
    ch = SerialBT.read();
    serialBuffer += ch;
  }
 
  if (ch == 'Q' && serialBuffer.length() > 0) 
  {
    
    for(int i=0; i<serialBuffer.length(); i++) 
    {
      if(serialBuffer.substring(i, i+1) == ",") 
      {
        actions[actionIndex] = serialBuffer.substring(lastIndex, i);
        lastIndex = i + 1;
        actionIndex++;
      }
      
      if(i == serialBuffer.length() - 1) 
      {
        actions[actionIndex] = serialBuffer.substring(lastIndex, i);
      }
    }
    
    serialBuffer = "";
    actionIndex = 0;
    lastIndex = 0;

    EnableStepper();
    homeXAxis();
    
    delay(1000);

    for(int z=0; z<30; z++) {
      if(actions[z] != "0") {
        parseInput(actions[z]);
      }
    }
    for(int y=0; y<30; y++) {
      actions[y] = "0";
    }
    
    SerialBT.println("OK");

    DisableStepper();
  }
}

void parseInput(String input)
{
  input.trim();
  byte command = input.charAt(0);
  switch(command) 
  {
    case 'H':
      homeXAxis();
      break;

    case 'X':
      moveXTo(input);
      break;

    case 'F':
      pour(input);
      break;
  }
}

void pour(String input)
{
  int count = 0;
  int times = 0;
  int holdDuration = 0;
  int waitDuration = 0;

  for(int z=0; z<input.length(); z++) 
  {
    byte parameter = input.substring(z, z+1).charAt(0);

    switch(parameter) 
    {
      case 'F':
        times = getParameterValue(input, z);
        break;

      case 'H':
        holdDuration = getParameterValue(input, z);
        break;

      case 'W':
        waitDuration = getParameterValue(input, z);
        break;
    }
  }
  
  if(holdDuration > 0 && waitDuration > 0) 
  {
    for(int i=0; i<times; i++) 
    {
      setRingUp(holdDuration);

      if(times - 1 > count) 
      {
        setRingDown(waitDuration, 0); 
      } 
      else 
      {
        setRingDown(waitDuration, 1); //last run: no delay in the end
      }
      count++;
    }
  } 
  else 
  {
    //Serial.println("Hold and wait duration parameters cannot be lower than or equal to 0");
  }
}

void moveX(bool directions, int steps, bool homeCommand)
{
  dir = directions;
  totalSteps = steps - 4800;
  accelcounter = ACCELERATIONDELAY;
  stepcounter = ACCELERATIONSTEP;
  drivecounter = 0;
  bool finished = false;
  
  while(!finished)
  {
    switch(state)  //idle, accelerate, moving, decelerate,
    {
      case accelerate:
      
                //Beschleunigen
                if(stepcounter > 0)
                {
                  if(accelcounter <= 0)
                  {
                    stp = !stp;
                    stepcounter--;
                    accelcounter = TOPSPEEDDELAY+(((stepcounter * stepcounter)/accelerationParameter));
                  }
                  else
                  {
                    accelcounter--;
                  }
                }
                else
                {
                  //Beschleunigung fertig
                  state = moving;
                  stepcounter = 0;
                }
    
                break;
      case moving:
                if(drivecounter <= 0)
                {
                  stp = !stp;
                  drivecounter = TOPSPEEDDELAY;
                  stepcounter++;
                }
                else
                {
                  drivecounter--;
                }
    
                if(stepcounter >= totalSteps || (homeCommand && stepcounter >= totalSteps + 4800))
                {
                  state = decelerate;
                  stepcounter = 0;
                  accelcounter = ACCELERATIONDELAY;
                }
    
                break;
                
      case decelerate:
                //Bremsen
                int accelStep = ACCELERATIONSTEP;
                if(homeCommand) accelStep = 2*ACCELERATIONSTEP;
                if(stepcounter < accelStep)
                {
                  if(accelcounter <= 0)
                  {
                    stp = !stp;
                    stepcounter++;
                    accelcounter = TOPSPEEDDELAY+(((stepcounter * stepcounter)/accelerationParameter));
                  }
                  else
                  {
                    accelcounter--;
                  }
                }
                else
                {
                  if(homeCommand) state = homeAxis;
                  //Bremsen fertig
                  finished = true;
                }
    
                break;

        case homeAxis:
                if(drivecounter <= 0)
                {
                  stp = !stp;
                  drivecounter = 40;
                }
                else
                {
                  drivecounter--;
                }
                break;

            
         
    }
    
     digitalWrite(OUTPUT_DIR, dir);
     digitalWrite(OUTPUT_STEP, stp);
     delayMicroseconds(10);
  }
}
void moveXTo(String input) 
{
  int pos = (input.substring(1).toInt())*-1;
  dir = false; //Nach links

  moveX(false, pos, false);

}

void setRingDown(int duration, char lastRun)
{
  digitalWrite(OUTPUT_ENABLERELAIS, LOW); //Disable Power
  delay(50);  //50ms switching time
  digitalWrite(OUTPUT_RELAIS, LOW); //Ring DOWN
  delay(50);
  digitalWrite(OUTPUT_ENABLERELAIS, HIGH); //Enable Power
  delay(600);
  digitalWrite(OUTPUT_ENABLERELAIS, LOW); //Disable Power after Ring moved down for 600ms
  delay(50);
  digitalWrite(OUTPUT_RELAIS, HIGH);  //Ring UP
  delay(50);
  digitalWrite(OUTPUT_ENABLERELAIS, HIGH);  //Enable Power after Switching up
  delay(400);
  digitalWrite(OUTPUT_ENABLERELAIS, LOW); //Disable Power after Ring moved up for 400ms
  delay(100);
  digitalWrite(OUTPUT_RELAIS, LOW); //Ring DOWN
  delay(50);
  digitalWrite(OUTPUT_ENABLERELAIS, HIGH); //Enable Power to move ring down completely
  
  if(lastRun)
  {
    delay(1300);
    digitalWrite(OUTPUT_ENABLERELAIS, LOW);
  }
  else
  {
    delay(1500);
    digitalWrite(OUTPUT_ENABLERELAIS, LOW); //Disable Power after Ring is down
    delay(duration - 2500); //Wait for next action
  }    
}

void setRingUp(int duration)
{
  digitalWrite(OUTPUT_ENABLERELAIS, LOW); //Disable Power
  delay(50);
  digitalWrite(OUTPUT_RELAIS, HIGH);  //Ring UP
  delay(50);
  digitalWrite(OUTPUT_ENABLERELAIS, HIGH); //Enable Power
  delay(1500);
  digitalWrite(OUTPUT_ENABLERELAIS, LOW); //Diable Power after Ring is up
  delay(duration - 500); //Wait for next action
}

int getParameterValue(String input, int z) 
{
  for(int y=z+1; y<input.length(); y++) 
  {
    if(input.substring(y, y+1) == " ") 
    {
      return input.substring(z+1, y).toInt();
      break;
    }

    if(y == input.length() - 1) 
    {
      return input.substring(z+1, y+1).toInt();
      break;
    }
  }

  return 0;
}

void EnableStepper(){
  //stepper.enableOutputs();
  digitalWrite(OUTPUT_SLEEP, HIGH);
}

void DisableStepper(){
  //stepper.disableOutputs();
  digitalWrite(OUTPUT_STEP, LOW);
  digitalWrite(OUTPUT_DIR, LOW);
  digitalWrite(OUTPUT_SLEEP, LOW);
  
}

void homeXAxis() 
{
  int endStopState = 0;
  if(! digitalRead(INPUT_ENDSWITCH))
  {
    delay(20);
    if(! digitalRead(INPUT_ENDSWITCH))
    {
      while (endStopState == 0) 
      {
//        stepper.moveTo(100);
//        stepper.setSpeed(HOME_SPEED);
//        stepper.runSpeed();
//        endStopState = digitalRead(INPUT_ENDSWITCH);
//        delay(2);
      }
      
      
//      while (stepper.distanceToGo() != 0) 
//      {
//        stepper.setSpeed(PARK_SPEED * -1);
//        stepper.runSpeed();
//      }
      
      endStopState = 0;
      
      while (endStopState == 0) 
      {
//        stepper.moveTo(100);
//        stepper.setSpeed(PARK_SPEED);
//        stepper.runSpeed();
        endStopState = digitalRead(INPUT_ENDSWITCH);
        delay(2);
      }

    }
  }
  
}
