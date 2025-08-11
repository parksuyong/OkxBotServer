# create_hash.py
from werkzeug.security import generate_password_hash
import sys

# 터미널에서 두 번째 인자(비밀번호)를 가져옴
if len(sys.argv) < 2:
    print("사용법: python create_hash.py '당신의 비밀번호'")
    sys.exit()

plain_password = sys.argv[1]
hashed_password = generate_password_hash(plain_password)

print("\n--- 암호화된 비밀번호 ---")
print(hashed_password)
print("--------------------------\n")