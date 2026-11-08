# 📱 HTML TO APK CONVERTER — Telegram Bot

HTML ওয়েবসাইট বা ZIP প্রজেক্টকে Telegram-এর ভেতর থেকেই Android APK-তে রূপান্তর করার বট।
Conversion আগের মতোই APK CreatorX build API (`/api/v1/build`) দিয়ে হয় — API, ফর্ম ফিল্ড (`app_name`, `html`, `icon`) ও লিমিট অপরিবর্তিত।

## ফাইল স্ট্রাকচার

| ফাইল | কাজ |
|---|---|
| `main.py` | বট চালু করার এন্ট্রি পয়েন্ট |
| `config.py` | সব সেটিং ও লিমিট (টোকেন শুধু Environment Variable থেকে) |
| `core.py` | লগিং (টোকেন redact হয়) ও বট অবজেক্ট |
| `handlers.py` | কমান্ড, বাটন, স্টেপ-ফ্লো, build পরিচালনা |
| `api_client.py` | Build API ক্লায়েন্ট (retry, stream download, error handling) |
| `validators.py` | নাম, HTML/ZIP, আইকনের ভ্যালিডেশন |
| `ui.py` | টেক্সট, বাটন, ব্যানার, স্ট্যাটাস স্ক্রিন |
| `sessions.py` | ইউজার সেশন, expiry, cooldown |
| `html_to_apk_banner.png` | ওয়েলকাম ব্যানার (না থাকলে বট নিজেই বানায়) |
| `Procfile`, `railway.toml`, `requirements.txt` | Railway ডেপ্লয়মেন্ট |

## Railway-তে ডেপ্লয় করার নিয়ম

1. এই ফোল্ডারের সব ফাইল একটি GitHub Repository-তে আপলোড করুন।
2. Railway → **New Project → Deploy from GitHub repo** → আপনার রিপোজিটরি সিলেক্ট করুন।
3. সার্ভিসের **Variables** ট্যাবে `BOT_TOKEN` যোগ করুন (@BotFather থেকে পাওয়া টোকেন)।
4. Deploy শেষ হলে **Deploy Logs**-এ `Logged in as @your_bot` এবং `Ready.` লেখা দেখবেন।
5. Telegram-এ বটে `/start` দিন।

> বটটি Long Polling ব্যবহার করে (worker), তাই কোনো পোর্ট বা ডোমেইন লাগে না।
> একই টোকেনে একসাথে একটির বেশি বট ইনস্ট্যান্স চালাবেন না।

## Environment Variables

| নাম | বাধ্যতামূলক | বিবরণ |
|---|---|---|
| `BOT_TOKEN` | ✅ | BotFather-এর টোকেন |
| `SUPPORT_URL` | ❌ | সেট করলে মেনুতে "💬 Support" বাটন আসে |
| `CHANNEL_URL` | ❌ | সেট করলে মেনুতে "📢 Channel" বাটন আসে |
| `MAX_CONCURRENT_BUILDS` | ❌ | একসাথে সর্বোচ্চ build (ডিফল্ট 3) |
| `BUILD_COOLDOWN_SECONDS` | ❌ | এক ইউজারের build-এর মধ্যে বিরতি (ডিফল্ট 10) |
| `SESSION_TTL_SECONDS` | ❌ | নিষ্ক্রিয় সেশন মুছে ফেলার সময় (ডিফল্ট 1800) |
| `BUILD_TIMEOUT` | ❌ | API উত্তরের অপেক্ষার সময়, সেকেন্ডে (ডিফল্ট 240) |
| `BUILD_RETRIES` | ❌ | সাময়িক ত্রুটিতে স্বয়ংক্রিয় রিট্রাই (ডিফল্ট 2) |
| `API_URL` | ❌ | ডিফল্ট: বর্তমান build API (সাধারণত পরিবর্তন করতে হয় না) |

## কমান্ড

`/start` `/menu` — মেইন মেনু • `/newapk` — নতুন APK • `/help` — কীভাবে কাজ করে • `/about` — পরিচিতি • `/cancel` — বাতিল

## লিমিট

- অ্যাপের নাম: ১–৮০ UTF-16 ইউনিট / ১২০ UTF-8 বাইট
- HTML / ZIP: সর্বোচ্চ ১০ MiB (ZIP-এ `index.html` থাকতে হবে)
- আইকন: PNG/JPEG (non-interlaced), সর্বোচ্চ ৬ MiB ও ৪ মেগাপিক্সেল
- তৈরি APK Telegram-এর ৫০ MB আপলোড সীমার মধ্যে থাকতে হবে

⚠️ এটি **টেস্ট বিল্ড**: প্রতিটি APK নতুন অস্থায়ী signing key দিয়ে সাইন হয়, তাই আগের বিল্ডকে আপডেট করা যায় না।
