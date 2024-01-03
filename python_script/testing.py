#!/usr/bin/env python3
# https://linuxconfig.org/how-to-work-with-the-woocommerce-rest-api-with-python

from woocommerce import API
# import json
import config

wc_api = API(
  url="https://emakerspace.ch/",
  consumer_key=config.consumer_key,
  consumer_secret=config.consumer_secret,
  timeout=50
)

# print(wc_api.get("orders").json())

data = {
    "status": "failed"
}

print(wc_api.put("orders/372", data).json())

# category_data = {
#     "name": "Example category222232225",
#     "description": "Just a category example2"
# }

# response = wc_api.post("products/categories", category_data)

# data = response.json()
# # print(data)
# print(response)
# if (response == 201):
#     id = data['id']
#     print("ID" + str(id))
# elif response == 400:
#     print("Already existing...")

# # json_string = json.loads(response)
# # print(json_string)
