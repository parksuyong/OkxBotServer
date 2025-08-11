from cryptography.fernet import Fernet

key = Fernet.generate_key()
print("새로운 암호화 키:")
print(key.decode())