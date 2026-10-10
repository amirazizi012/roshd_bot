# استقرار ربات تکالیف رشد روی Northflank (صفر تا صد)

> منبع: مستندات رسمی Northflank (Build with a Dockerfile، Combined service، Volumes، Environment variables). نام دکمه‌ها ممکن است کمی فرق کند.

## ۰) پیش‌نیازها

| مورد | توضیح |
|---|---|
| Git | از git-scm.com نصب کن |
| حساب Git (GitHub / GitLab / Bitbucket) | Northflank از همین‌ها کد را می‌خواند. **مخزن را Private بساز** |
| حساب Northflank | ثبت‌نام و (در صورت نیاز) تأیید |
| توکن **جدید** بله | توکن قبلی لو رفته؛ در BotFather بازسازی کن |

## ۱) مشکل صفحهٔ «No Dockerfile found at /Dockerfile»

یعنی در **ریشهٔ مخزن** فایل `Dockerfile` نیست؛ معمولاً چون پوشهٔ بالاتر را آپلود کرده‌ای و پروژه یک پوشه پایین‌تر افتاده. راه‌حل: Git را **داخل خود پوشهٔ پروژه** (همان‌جا که `Dockerfile` و `requirements.txt` هستند) بزن:

```bash
cd C:\Users\Lenovo\Desktop\roshd_bot
git init
git add .
git status
```

قبل از commit دقت کن در لیست `.env`، `data/` و `.venv/` **نباشند** (فایل `.gitignore` آماده در پروژه این‌ها را کنار می‌گذارد). بعد:

```bash
git commit -m "first version"
git branch -M main
git remote add origin https://github.com/USERNAME/roshd-bot.git
git push -u origin main
```

در سایت Git باید `Dockerfile` را بلافاصله در صفحهٔ اول مخزن ببینی. اگر پروژه یک پوشه پایین‌تر بود (مثلاً `roshd_bot/Dockerfile`)، در Northflank هر دو مسیر را عوض کن: Build context = `/roshd_bot` و Dockerfile location = `/roshd_bot/Dockerfile`.

## ۲) ساخت پروژه

Northflank ← Create project ← نام `roshd` ← Region: نزدیک‌ترین به ایران (اروپا) ← Create.

## ۳) ساخت سرویس (Combined service)

1. Create new ← **Service** ← **Combined** (هم می‌سازد هم اجرا می‌کند) ← نام: `roshd-bot`.
2. Repository: حساب Git را وصل کن، مخزن و شاخهٔ `main` را بزن.
3. Build options (همان صفحهٔ تو):

| فیلد | مقدار |
|---|---|
| Build type | Dockerfile |
| Build context | `/` |
| Dockerfile location | `/Dockerfile` |

بعد از درست شدن مسیر، دکمهٔ **Reload** محتوای Dockerfile را نشان می‌دهد؛ دیدن آن یعنی درست است.

4. Networking / Ports: یک پورت `3000` با پروتکل HTTP اضافه کن (پنل وب). گزینهٔ Public را برای شروع **خاموش** بگذار (بخش ۹).
5. Resources: **کوچک‌ترین پلن** و تعداد نمونه (instances) = **۱**.
6. Environment (متغیرهای محیطی، بخش بعد) را همین‌جا یا بعد از ساخت وارد کن.
7. Create service. اولین build خودکار شروع می‌شود.

## ۴) متغیرهای محیطی (Environment ← Runtime variables)

| نام | مقدار |
|---|---|
| `BALE_TOKEN` | توکن جدید |
| `ADMIN_IDS` | فعلاً خالی (بعد از `/myid` پر می‌شود) |
| `PANEL_PASSWORD` | رمز طولانی (حداقل ۱۶ نویسه) |
| `PANEL_HOST` | `0.0.0.0` |
| `PANEL_PORT` | `3000` |
| `DATA_DIR` | `/data` |
| `SQLITE_JOURNAL` | `DELETE` |
| `DIAG_ON_START` | `1` (فقط برای تست اتصال) |

## ۵) دیسک دائمی (بسیار مهم)

دیسک موقت کانتینر با هر ری‌استارت و استقرار پاک می‌شود. سرویس ← **Volumes** ← Add volume:

| فیلد | مقدار |
|---|---|
| Name | `roshd-data` |
| Size | ۱ گیگابایت (بعداً فقط قابل افزایش است، نه کاهش) |
| Container mount path | `/data` |

ذخیره کنی، سرویس خودکار دوباره اجرا می‌شود. اگر پلن رایگان دیسک را نداد، **متوقف شو** و پیام خطا را برایم بفرست؛ بدون دیسک، دیتابیس با هر ری‌استارت پاک می‌شود.

## ۶) تست اتصال به بله (مهم‌ترین مرحله)

سرویس ← Logs. بعد از build و استقرار، باید خروجی `--diag` را ببینی:

| در لاگ | معنی و کار بعدی |
|---|---|
| `[OK ] direct` و `connected as @...` | عالی؛ ادامه بده |
| `[FAIL] direct ... ConnectTimeout / Timeout / Reset` | احتمالاً بله اتصال از خارج ایران را نمی‌پذیرد. لاگ را برایم بفرست؛ باید یا پروکسی ایرانی (`BALE_PROXY`) یا سرور ایرانی بگیریم |
| `401` / `Unauthorized` | توکن اشتباه است |

بعد از موفقیت، متغیر `DIAG_ON_START` را حذف کن.

## ۷) مدیر شدن و تست

1. در بله به ربات `/myid` بزن و عدد را در `ADMIN_IDS` بگذار (سرویس ری‌استارت می‌شود).
2. چک‌لیست ۱۰ مرحله‌ای تست دستی در `docs/راهنمای-نصب-لوکال.md` را برو.
3. تست ماندگاری: یک گروه بساز، سرویس را Restart کن؛ گروه باید بماند.

## ۸) به‌روزرسانی

در سرویس ترکیبی، با هر `git push` روی `main` خودکار build و استقرار انجام می‌شود:

```bash
git add .
git commit -m "توضیح تغییر"
git push
```

## ۹) پنل وب

- **ایمن‌تر:** پورت Public خاموش بماند و پنل را با Northflank CLI از کامپیوتر خودت وصل کنی (در داشبورد سرویس بخش *Local access* دستور آماده می‌دهد).
- **ساده‌تر:** پورت ۳۰۰۰ را Public کن؛ آدرس HTTPS از Northflank می‌گیری. چون نام و پاسخ بچه‌ها در پنل است، رمز طولانی و منحصربه‌فرد بگذار.
- اختیاری: در Health checks یک بررسی HTTP روی مسیر `/healthz` و پورت ۳۰۰۰ تعریف کن.

## ۱۰) بکاپ

هر ۲۴ ساعت یک بکاپ از دیتابیس برای مدیر در بله می‌آید؛ `/backup` بکاپ فوری می‌دهد. عکس‌های پاسخ در بکاپ نیست.

## ۱۱) عیب‌یابی

| نشانه | علت و راه‌حل |
|---|---|
| `No Dockerfile found` | بخش ۱ را ببین؛ Dockerfile در ریشهٔ مخزن (یا مسیر درست) نیست |
| build شکست می‌خورد | لاگ build را بفرست (معمولاً دانلود بسته‌ها یا نسخهٔ Python) |
| سرویس مدام ری‌استارت می‌شود | لاگ runtime؛ معمولاً متغیر جا افتاده یا توکن اشتباه است |
| بعد از ری‌استارت داده‌ها پاک شد | دیسک روی `/data` وصل نیست یا `DATA_DIR` تنظیم نشده |
| پیام‌های ربات تکراری است | نمونهٔ لوکال هنوز روشن است یا بیش از یک instance دارید |
| سرویس ناگهان بسته می‌شود | احتمالاً کمبود حافظه؛ منابع را در Metrics ببین و پلن را بالا ببر |

## ۱۲) امنیت

مخزن Private، هیچ رمزی در Git نباشد (`git ls-files` را بزن و مطمئن شو `.env` نیست)، توکن بازسازی شده باشد، و رمز پنل طولانی باشد.
