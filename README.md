# eMakerspace-BarBot

Design your own drinks...
- Use a WordPress configurator to design a drink
- Pay the drink with Twint (WooCommerce Plugin)
- Grab the data with a python script over the WooCommerce API
- Control the robot and make the drinks

![Configurator](doc/online_config.png)

## Processing an order
```
━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━
   444 ┃ Marco                         ┃ processing
━━━━━━━╋━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━╋━━━━━━━━━━━━━━
    ID ┃ Name       ┃ Filler           ┃ Alc
───────╂────────────╂──────────────────╂──────────────
     0 ┃ Vodka-O    ┃ Orangensaft      ┃ Vodka
     1 ┃ DIY drink  ┃ Citro            ┃ Tequila

Process order 444
Process drink  0/1: Vodka-O
Robot: Waiting for a new cup
Robot: Start movement
Robot: Pouring filler: Orangensaft
Robot: Pouring alcohol: Vodka
Robot: Completed Vodka-O
Process drink  1/1: DIY drink
Robot: Waiting for a new cup
Robot: Start movement
Robot: Pouring filler: Citro
Robot: Pouring alcohol: Tequila
Robot: Completed DIY drink
Completed order 444, upload new status to WordPress
Upload completed
```

### Limitations
No umlauts allowed in the drinks 
