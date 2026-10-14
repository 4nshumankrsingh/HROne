import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
client = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=8000)
print(client.admin.command("ping"))
print("server version:", client.server_info()["version"])