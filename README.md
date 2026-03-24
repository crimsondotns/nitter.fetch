# Twitter Scraper Modernized (V2)

ระบบ Python Scraper สำหรับดึงข้อมูลสถิติ (Stats) และโพสต์ล่าสุด (Tweets) จาก Twitter/X แล้วซิงค์ลง Google Sheets อัตโนมัติทุกเช้า

## 📁 โครงสร้างไฟล์หลัก

1.  **`stats.py`**: ดึงข้อมูลสถิติโปรไฟล์ (Followers, Posts, Community Members) ผ่าน X API บันทึกลงชีต `User_on_X`
2.  **`post.py`**: ดึงโพสต์ล่าสุดผ่าน Nitter RSS ในรูปแบบ **Wide Format** (1 คนต่อ 1 แถว เรียงโพสต์ออกทางขวา) บันทึกลงชีต `Migration`
3.  **`common.py`**: ศูนย์รวมฟังก์ชัน Log สวยๆ (Aesthetic Logger), ระบบ Network Backoff, และการจัดการ Google Sheets

## ✨ ฟีเจอร์เด่น

-   **Aesthetic Logging:** แสดงผลใน Console สวยงามพร้อมบอก Row และ Account อย่างชัดเจน
-   **Stealth Delay:** ระบบหน่วงเวลาแบบสุ่ม (5-20 วินาที) เพื่อความปลอดภัยสูงสุด
-   **Wide Output & Dense Packing:** จัดเก็บข้อมูลโพสต์แบบแนวนอน และมีระบบ Bulk Overwrite เพื่อให้ชีทไม่มีแถวว่าง
-   **Daily Automation:** ตั้งค่า GitHub Actions ให้รันอัตโนมัติ **ทุกวันเวลา 07:00 น. (ไทย)**
-   **Sync Timestamp:** ระบบจะเขียนเวลาที่รันเสร็จล่าสุดลงที่ **Cell G1** ของชีต `Migration` อัตโนมัติ

## ⏱️ ประมาณการความเร็ว (Performance Estimate)
สำหรับการตั้งค่า Stealth Delay (เฉลี่ย 15 วินาที/คน):
-   **100 บัญชี:** ~25 นาที
-   **500 บัญชี:** ~2 ชั่วโมง
-   **1,000 บัญชี:** ~4 ชั่วโมง 10 นาที
-   **1,440 บัญชี:** ~6 ชั่วโมง (ขีดจำกัดสูงสุดที่แนะนำ)

## 🚀 การติดตั้งและใช้งาน

### 1. ติดตั้ง Dependencies
```bash
pip install requests gspread google-auth
```

### 2. ตั้งค่า Environment (.env)
```env
X_BEARER="your_token"
X_COOKIE_STRING="your_cookies"
TELEGRAM_BOT_TOKEN="your_bot"
TELEGRAM_CHAT_ID="your_id"
```

### 3. รันสคริปต์
```bash
python stats.py  # ดึงสถิติโปรไฟล์
python post.py   # ดึงโพสต์ล่าสุด
```
