# PSEditor

โปรแกรมแก้ไขโค้ดสไตล์ Cursor ที่ทำงานในเว็บเบราว์เซอร์ สร้างด้วย Django
มี Monaco editor (เอนจินเดียวกับ VS Code), ตัวจัดการไฟล์ (Explorer), การค้นหา, เทอร์มินัลในตัว และหน้าต่าง AI Chat
ที่เชื่อมต่อกับ **agent ภายนอก** (ค่าเริ่มต้นคือ [OpenRouter](https://openrouter.ai)) เพื่อให้สร้างและแก้ไขโค้ดใน workspace ของคุณได้

> PSEditor ออกแบบมาให้ใช้ **บนเครื่องของคุณเองเท่านั้น** (`http://127.0.0.1:8800/`)

เอกสารอื่น ๆ
- [`USER_MANUAL_TH.md`](USER_MANUAL_TH.md) คู่มือการใช้งานฉบับเต็ม (ภาษาไทย)
- [`OPENROUTER_GUIDE_TH.md`](OPENROUTER_GUIDE_TH.md) วิธีสมัคร OpenRouter และขอ API key
- [`USER_MANUAL.md`](USER_MANUAL.md) คู่มือการใช้งานฉบับภาษาอังกฤษ

---

## สารบัญ

1. [สิ่งที่ต้องมี](#สิ่งที่ต้องมี)
2. [การติดตั้ง](#การติดตั้ง)
3. [การรัน PSEditor](#การรัน-pseditor)
4. [เริ่มใช้งานครั้งแรก](#เริ่มใช้งานครั้งแรก)
5. [การอนุมัติการทำงานของ agent](#การอนุมัติการทำงานของ-agent)
6. [เทอร์มินัล](#เทอร์มินัล)
7. [ประเภทของ agent](#ประเภทของ-agent)
8. [ความปลอดภัย](#ความปลอดภัย)
9. [การทดสอบ](#การทดสอบ)
10. [โครงสร้างโปรเจกต์](#โครงสร้างโปรเจกต์)

---

## สิ่งที่ต้องมี

| รายการ | ข้อกำหนด |
|--------|----------|
| ระบบปฏิบัติการ | Windows 10 หรือ 11 แบบ 64 บิต หรือ **macOS** (ชิป Apple M1–M4 และ Intel) |
| Python | 3.10 ขึ้นไป (แนะนำ 3.13) ถ้ายังไม่มี ตัวติดตั้งจะช่วยติดตั้งให้ผ่าน `winget` (Windows) หรือ Homebrew (macOS) |
| เบราว์เซอร์ | Chrome, Edge, Firefox หรือ Safari รุ่นใหม่ |
| อินเทอร์เน็ต | ต้องใช้สำหรับ AI agent และตอนเปิดหน้าเว็บครั้งแรก (โหลด Monaco / xterm.js จาก CDN) ถ้าไม่มี PSEditor จะใช้ตัวแก้ไขข้อความธรรมดาแทน |
| API key | OpenRouter API key จาก <https://openrouter.ai/keys> (ดู [`OPENROUTER_GUIDE_TH.md`](OPENROUTER_GUIDE_TH.md)) |

---

## การติดตั้ง

| | Windows | macOS |
|---|---|---|
| ชุดติดตั้ง | `PSEditor-install.zip` | `PSEditor-mac.zip` |
| ติดตั้ง | ดับเบิลคลิก `install.bat` | คลิกขวา `install_mac.command` → **Open** |
| เปิดโปรแกรม | `run.bat` หรือทางลัด **PSEditor** | `run_mac.command` หรือ **PSEditor.command** บนเดสก์ท็อป |
| ปิดโปรแกรม | `Ctrl+C` ในหน้าต่างคอนโซล | `Ctrl+C` ในหน้าต่าง Terminal |

### วิธีที่ 1: ติดตั้งด้วย `install.bat` บน Windows (แนะนำ)

1. แตกไฟล์ `PSEditor-install.zip` ไปยังโฟลเดอร์ที่เขียนไฟล์ได้ เช่น `C:\PSEditor` หรือ `F:\pseditor`
   (หลีกเลี่ยง `C:\Program Files`)
2. เปิดโฟลเดอร์นั้น แล้วดับเบิลคลิก **`install.bat`**
3. ตัวติดตั้งจะทำงานโดยอัตโนมัติ
   - ค้นหา Python 3.10 ขึ้นไป ถ้าไม่พบจะถามว่าจะติดตั้ง Python 3.13 ผ่าน `winget` หรือไม่ (ตอบ `Y`)
   - สร้าง virtual environment `.venv`
   - ติดตั้งแพ็กเกจ Python จากโฟลเดอร์ `wheels` **โดยไม่ต้องใช้อินเทอร์เน็ต**
   - สร้าง/อัปเดตฐานข้อมูล `db.sqlite3`
   - ถาม **OpenRouter API key** (ตัวอักษรที่พิมพ์จะถูกซ่อน กด **Enter** เพื่อข้ามแล้วใส่ภายหลังได้)
   - สร้างทางลัด **PSEditor** บนเดสก์ท็อปและใน Start menu
4. เมื่อเสร็จ ตอบ `Y` เพื่อเปิด PSEditor ทันที

บันทึกการติดตั้งอยู่ในไฟล์ `install.log` หากเกิดปัญหาให้เปิดอ่านไฟล์นี้
รัน `install.bat` ซ้ำได้อย่างปลอดภัย ข้อมูลเดิมไม่หาย

### วิธีที่ 2: ติดตั้งด้วย `install_mac.command` บน macOS

1. ติดตั้ง Python 3.10 ขึ้นไปจาก <https://www.python.org/downloads/macos/> หรือ `brew install python@3.13`
   (ถ้ามี Homebrew ตัวติดตั้งจะถามว่าจะให้ติดตั้งให้หรือไม่)
2. ดับเบิลคลิก `PSEditor-mac.zip` ใน Finder จะได้โฟลเดอร์ **PSEditor** ย้ายไปไว้ในที่ถาวร เช่นโฟลเดอร์ home
3. เปิดโฟลเดอร์ แล้ว **คลิกขวาที่ `install_mac.command` → Open** แล้วคลิก **Open** ในหน้าต่างเตือน
   (ครั้งแรก macOS จะบล็อกการดับเบิลคลิกไฟล์ที่มาจากเครื่องอื่น ถ้ายังเปิดไม่ได้ ให้ไปที่
   **System Settings → Privacy & Security → Open Anyway** หรือรัน `bash ~/PSEditor/install_mac.command` ใน Terminal)
4. ตัวติดตั้งทำขั้นตอนเดียวกับบน Windows: สร้าง `.venv`, ติดตั้งแพ็กเกจจากโฟลเดอร์ `wheels_mac` **โดยไม่ต้องใช้อินเทอร์เน็ต**,
   สร้าง/อัปเดต `db.sqlite3`, ถาม OpenRouter API key และสร้าง **PSEditor.command** บนเดสก์ท็อป
5. เมื่อเสร็จ ตอบ `y` เพื่อเปิด PSEditor ทันที

บันทึกการติดตั้งอยู่ในไฟล์ `install.log` เช่นกัน และรันซ้ำได้อย่างปลอดภัย

### วิธีที่ 3: ติดตั้งด้วยตนเอง (สำหรับนักพัฒนา)

Windows (PowerShell หรือ Command Prompt ในโฟลเดอร์ของ PSEditor)

```powershell
cd F:\pseditor

# สร้างและเปิดใช้งาน virtual environment (ไม่บังคับ แต่แนะนำ)
python -m venv .venv
.venv\Scripts\activate

# ติดตั้งแพ็กเกจและสร้างฐานข้อมูล
pip install -r requirements.txt
python manage.py migrate
```

macOS (Terminal)

```bash
cd ~/PSEditor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
```

`requirements.txt` ติดตั้ง `pywinpty` เฉพาะบน Windows บน macOS ไม่ต้องใช้แพ็กเกจเพิ่ม

### สร้างชุดติดตั้งเพื่อนำไปใช้เครื่องอื่น

| ไฟล์ที่ต้องการ | ดับเบิลคลิก | ผลลัพธ์ |
|---------------|------------|---------|
| ตัวโปรแกรมอย่างเดียว (ไม่มีข้อมูลส่วนตัว) | `package.bat` | `dist\PSEditor-install.zip` |
| ตัวโปรแกรม **พร้อม**ข้อมูล, agent และ API key (Windows) | `package_full.bat` | `dist\PSEditor-full.zip` |
| ตัวโปรแกรมสำหรับ **macOS** (ไม่มีข้อมูลส่วนตัว) | `package_mac.bat` | `dist\PSEditor-mac.zip` |

สคริปต์ทั้งหมดรันบน Windows และต้องใช้อินเทอร์เน็ตเพื่อดาวน์โหลดแพ็กเกจ Python แบบออฟไลน์
(`wheels\` สำหรับ Windows, `wheels_mac\` สำหรับ macOS ทั้งชิป arm64 และ x86_64, Python 3.10–3.14)
`package_mac.bat` เรียก `package_mac.py` ซึ่งสร้าง zip ด้วย `zipfile` เพื่อให้ไฟล์ `.command` ยังรันได้
(ขึ้นบรรทัดแบบ LF และมีสิทธิ์ execute) เมื่อแตกไฟล์บน Mac

ถ้าต้องการย้ายข้อมูลเดิมไป Mac ให้คัดลอก `db.sqlite3` ไปวางในโฟลเดอร์ `PSEditor` ที่แตกไฟล์แล้ว ก่อนรัน `install_mac.command`

> `PSEditor-full.zip` และ `db.sqlite3` มี API key ของคุณอยู่ข้างใน อย่าแชร์ให้ผู้อื่น

---

## การรัน PSEditor

### วิธีที่ 1: ดับเบิลคลิก (แนะนำ)

ดับเบิลคลิกทางลัด **PSEditor** บนเดสก์ท็อป หรือไฟล์ **`run.bat`** ในโฟลเดอร์ PSEditor

- จะมีหน้าต่างคอนโซลสีดำเปิดขึ้น และเบราว์เซอร์จะเปิด <http://127.0.0.1:8800/> เองภายในประมาณ 2 วินาที
  (ถ้าไม่เปิด ให้พิมพ์ที่อยู่นี้ในเบราว์เซอร์)
- `run.bat` จะอัปเดตฐานข้อมูล (`migrate`) ให้ก่อนทุกครั้ง

บน **macOS** ดับเบิลคลิก **PSEditor.command** บนเดสก์ท็อป หรือ **`run_mac.command`** ในโฟลเดอร์ PSEditor
จะเปิดหน้าต่าง Terminal และเบราว์เซอร์ให้แบบเดียวกัน

### วิธีที่ 2: รันด้วยคำสั่ง

```powershell
# Windows
cd F:\pseditor
.venv\Scripts\activate
python manage.py migrate
python manage.py runserver 127.0.0.1:8800
```

```bash
# macOS
cd ~/PSEditor
source .venv/bin/activate
python manage.py migrate
python manage.py runserver 127.0.0.1:8800
```

จากนั้นเปิด <http://127.0.0.1:8800/> ในเบราว์เซอร์

### การปิดโปรแกรม

ไปที่หน้าต่างคอนโซลสีดำ (หรือหน้าต่าง Terminal บน macOS) แล้วกด **Ctrl+C** หรือปิดหน้าต่างนั้น
**ต้องเปิดหน้าต่างนี้ค้างไว้ตลอดเวลาที่ใช้งาน** ถ้าปิดแล้วโปรแกรมจะหยุดทำงาน

> เปลี่ยนพอร์ตได้โดยแก้ `8800` ใน `run.bat` ทั้งสองตำแหน่ง (บน macOS แก้ใน `run_mac.command` ทั้งสองตำแหน่ง)

---

## เริ่มใช้งานครั้งแรก

1. **เปิดโฟลเดอร์** (มุมซ้ายบน หรือ **Ctrl+Shift+O**) เลือกโฟลเดอร์ใดก็ได้ในเครื่อง หรือสร้างโฟลเดอร์ใหม่
   สามารถ **เพิ่มโฟลเดอร์เข้า workspace** ได้ด้วย **Add to workspace** (ใน Explorer) หรือ **+ Add folder** (แถบ *Workspace* ในแชท)
   Workspace คือชุดโฟลเดอร์ที่ agent อ่าน เขียน แก้ไข และลบไฟล์ได้เท่านั้น โฟลเดอร์ที่ไม่ได้เพิ่มเข้ามา agent จะเข้าถึงไม่ได้
   ถ้ามีหลายโฟลเดอร์ path จะแสดงเป็น `ชื่อโฟลเดอร์/path/ของ/ไฟล์`
   นำโฟลเดอร์ออกด้วย ✕ ที่ชื่อโฟลเดอร์ หรือคลิกขวาใน Explorer (ไฟล์บนดิสก์ไม่ถูกลบ)
   ไฟล์ที่ยังไม่บันทึกจะถูกบันทึกให้ก่อนเปลี่ยนรายการโฟลเดอร์
2. **เชื่อมต่อ agent** คลิก **⚙ Agents** → เลือก **OpenRouter** → วาง API key → **Save** → **Test connection**
   (วิธีขอ key ดู [`OPENROUTER_GUIDE_TH.md`](OPENROUTER_GUIDE_TH.md))
3. พิมพ์คำสั่งในแผง **AI Chat** (ด้านขวา) agent สามารถแสดงรายการ/อ่าน/ค้นหาไฟล์, สร้าง/แก้ไข/ลบไฟล์ และรันคำสั่งได้
   **คุณต้องอนุมัติทุกการกระทำที่เปลี่ยนแปลงข้อมูล** ไฟล์ที่เปลี่ยนจะโหลดใหม่ในตัวแก้ไข
   และทุกคำสั่งจะมีปุ่ม **Revert changes** สำหรับย้อนกลับ

### คีย์ลัด

| คีย์ลัด | การทำงาน |
|--------|---------|
| `Ctrl+S` | บันทึกไฟล์ |
| `Ctrl+P` | ไปยังไฟล์ |
| `Ctrl+L` | ไปที่ช่องพิมพ์ AI Chat |
| ``Ctrl+` `` | แสดง/ซ่อนเทอร์มินัล |
| `Ctrl+Shift+O` | เปิดโฟลเดอร์ |
| `Ctrl+Shift+F` | ค้นหาในไฟล์ |

ภายในเทอร์มินัล มีเพียง ``Ctrl+` `` ที่ PSEditor รับไว้ ส่วน Ctrl+P / Ctrl+L / Ctrl+S จะส่งไปที่ shell

บน macOS ใช้ปุ่ม **Cmd** แทน **Ctrl** ได้ (เช่น `Cmd+S`, `Cmd+P`)

---

## การอนุมัติการทำงานของ agent

ก่อนที่ agent จะเขียน แก้ไข หรือลบไฟล์ หรือรันคำสั่ง แชทจะแสดงสิ่งที่ agent ต้องการทำ
(เนื้อหาไฟล์, ข้อความก่อน/หลังแก้ไข หรือคำสั่งที่จะรันจริง) พร้อมปุ่ม **Approve** และ **Deny**

- จะไม่มีอะไรทำงานจนกว่าคุณจะคลิก **Approve**
- **Deny** (ใส่เหตุผลหรือ "ให้ทำแบบนี้แทน" ได้) agent จะรับทราบและปรับการทำงาน
- **Stop** ยกเลิกการกระทำที่รออยู่
- ถ้าไม่ตอบ การกระทำนั้นจะ **ไม่ถูกทำ** และหมดเวลาใน 10 นาที
- การอ่านไฟล์ (`list_files`, `read_file`, `search`) ไม่ต้องอนุมัติ
- agent แบบ webhook ถามทีละการกระทำ ส่วนแบบ command-line (ที่แก้ไฟล์เอง) อนุมัติครั้งเดียวก่อนเริ่ม

ตั้งค่าได้ใน `pseditor/settings.py`: `PSEDITOR_REQUIRE_APPROVAL` (ค่าเริ่มต้น `True`),
`PSEDITOR_APPROVE_READS` (`True` = ถามก่อนอ่านไฟล์ด้วย) และ `PSEDITOR_APPROVAL_TIMEOUT`
การรออนุมัติเกิดขึ้นภายในคำขอแชท จึงต้องรันเซิร์ฟเวอร์แบบ threaded (`runserver` เป็นแบบนี้อยู่แล้ว ถ้าใช้ gunicorn ให้ใส่ `--threads`)

---

## เทอร์มินัล

กด ``Ctrl+` `` เพื่อเปิด เป็นเทอร์มินัลจริง (xterm.js บนเบราว์เซอร์ และ pseudo-terminal บนเซิร์ฟเวอร์ ใช้ ConPTY บน Windows
และโมดูล `pty` ของ Python บน macOS) หนึ่งตัวต่อหนึ่ง workspace รัน PowerShell บน Windows และ bash บน macOS
โปรแกรมเห็นเป็น TTY จริง จึงใช้งานแบบโต้ตอบได้
เช่น `python manage.py createsuperuser`, `input()`, การถามรหัสผ่าน, Python REPL, prompt ของ `pip`/`npm`, สี, ประวัติคำสั่งด้วยลูกศร และ `Ctrl+C`

- prompt แสดงโฟลเดอร์จริง (เช่น `C:\projects\app>` บน Windows หรือ `~/projects/app$` บน macOS) และ `cd` / `ls` / `dir` ใช้ได้ทุกที่ในเครื่อง
- shell จำสถานะ (โฟลเดอร์ ตัวแปร virtualenv ที่ activate) จนกว่าจะปิด และทำงานต่อแม้รีโหลดหน้าหรือซ่อนแผง
- พิมพ์ `exit` เพื่อจบ shell กดปุ่มใดก็ได้หลังจากนั้นเพื่อเริ่มใหม่
- เลือกข้อความแล้วกด `Ctrl+C` เพื่อคัดลอก (ถ้าไม่เลือกข้อความ จะเป็น `Ctrl+C` จริง) และ `Ctrl+Shift+V` เพื่อวาง
- activate virtualenv บน Windows ได้หลายแบบ เช่น `venv\Scripts\activate`, `.venv\Scripts\activate`, `source venv/Scripts/activate`
  บน macOS ใช้ `source .venv/bin/activate` prompt จะมี `(venv)` นำหน้า และ `deactivate` ใช้ได้เช่นกัน

เทอร์มินัลต้องใช้แพ็กเกจ `pywinpty` บน Windows (`pip install -r requirements.txt`) บน macOS ไม่ต้องใช้แพ็กเกจเพิ่ม
และโหลด xterm.js จาก CDN เดียวกับ Monaco
ถ้าขาดอย่างใดอย่างหนึ่ง PSEditor จะใช้เทอร์มินัลแบบง่ายที่รับคำสั่งทีละบรรทัดแทน
(ใช้โปรแกรมโต้ตอบไม่ได้ แต่ยังจำ `cd` ตัวแปร และ venv ระหว่างคำสั่ง)

---

## ประเภทของ agent

| ประเภท | ใช้กับ | หมายเหตุ |
|--------|-------|---------|
| OpenAI-compatible | **OpenRouter (ค่าเริ่มต้น)**, OpenAI, Ollama, LM Studio, vLLM, Groq, … | `base_url` เช่น `https://openrouter.ai/api/v1` ต้องใช้โมเดลที่รองรับ tool calling |
| Anthropic | Claude ผ่าน Messages API | รองรับ tool use ในตัว |
| Custom HTTP (webhook) | บริการ agent ของคุณเอง | ดูโปรโตคอลด้านล่าง |
| Command-line | `claude -p`, `codex exec`, `aider`, สคริปต์ใดก็ได้ | รันในโฟลเดอร์ workspace ส่ง prompt ทาง stdin หรือแทนที่ `{prompt}` ตรวจจับไฟล์ที่เปลี่ยนด้วย snapshot และย้อนกลับได้ |

preset ของ CLI เป็นเพียงตัวอย่าง ควรตรวจสอบ flag ของเครื่องมือเวอร์ชันที่ติดตั้งอยู่

### โปรโตคอล webhook

PSEditor จะ `POST` JSON ไปที่ URL ของคุณ (พร้อม `Authorization: Bearer <key>` ถ้ามีการตั้ง key)

```json
{
  "instruction": "add a /health endpoint",
  "messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
  "system_prompt": "...",
  "model": "",
  "workspace": {"name": "myapp", "path": "D:\\projects\\myapp"},
  "files": ["app.py", "..."],
  "active_file": {"path": "app.py", "selection": "", "content": "..."}
}
```

และคาดหวังการตอบกลับดังนี้

```json
{
  "reply": "Added the endpoint.",
  "actions": [
    {"type": "write_file", "path": "app.py", "content": "..."},
    {"type": "edit_file", "path": "app.py", "old_string": "a", "new_string": "b"},
    {"type": "delete_file", "path": "old.py"},
    {"type": "run_command", "command": "pytest -q"}
  ]
}
```

---

## ความปลอดภัย

PSEditor อ่านและเขียนไฟล์ในโฟลเดอร์ที่คุณเปิด และรันคำสั่ง shell ได้ จึงออกแบบให้ **ใช้บนเครื่องตัวเองเท่านั้น**
`ALLOWED_HOSTS` จำกัดไว้เฉพาะ localhost อย่าเปิดให้เครือข่ายภายนอกเข้าถึง

- การเข้าถึงไฟล์ถูกจำกัดอยู่ในโฟลเดอร์ของ workspace (ปฏิเสธ path traversal) เฉพาะผู้ใช้เท่านั้นที่เพิ่มโฟลเดอร์ได้
  โมเดลมีเครื่องมือ `workspace_info` แบบอ่านอย่างเดียว แต่ขยายสิทธิ์ของตัวเองไม่ได้
- API key ถูกเก็บเป็น **ข้อความธรรมดา** ใน `db.sqlite3` อย่าแชร์ไฟล์นี้หรือ `PSEditor-full.zip`
- **เทอร์มินัลเป็น shell จริงและไม่ถูกจำกัดอยู่ใน workspace** ทำได้ทุกอย่างที่ผู้ใช้ Windows หรือ macOS ของคุณทำได้
  ส่วน tool `run_command` และเครื่องมือไฟล์ของ agent ยังถูกจำกัดอยู่ในโฟลเดอร์ workspace
- ตั้ง `PSEDITOR_ALLOW_TERMINAL = False` ใน `pseditor/settings.py` เพื่อปิดเทอร์มินัลและ `run_command` ของ agent
- agent ทำงานได้หลังจากคุณอนุมัติทุกการเขียน/แก้ไข/ลบ/รันคำสั่ง การแก้ไขที่อนุมัติแล้วจะบันทึกลงดิสก์ทันที
  ใช้ **Revert changes** หรือ git เพื่อย้อนกลับ

---

## การทดสอบ

```powershell
# Windows
.venv\Scripts\python manage.py test editor
```

```bash
# macOS
.venv/bin/python manage.py test editor
```

การทดสอบที่ใช้ PowerShell โดยเฉพาะจะถูกข้ามบน macOS

---

## โครงสร้างโปรเจกต์

- `install.bat`, `install.ps1` ตัวติดตั้ง (Windows)
- `install_mac.command` ตัวติดตั้ง (macOS)
- `run.bat` / `run_mac.command` ตัวเปิดโปรแกรม (Windows / macOS)
- `package.bat`, `package_full.bat` สร้างชุดติดตั้ง Windows
- `package_mac.bat`, `package_mac.py` สร้างชุดติดตั้ง macOS
- `editor/migrations/0003_default_openrouter_agent.py` สร้าง agent OpenRouter เป็นค่าเริ่มต้นในการติดตั้งใหม่
- `editor/fsops.py` การทำงานกับไฟล์อย่างปลอดภัย, การค้นหา, ตัวรันโปรเซส
- `editor/ptyterm.py` เทอร์มินัลจริง (PTY) ที่อยู่เบื้องหลัง xterm.js
- `editor/shell.py` shell แบบบรรทัดต่อบรรทัด (เทอร์มินัลสำรอง)
- `editor/tools.py` เครื่องมือของ agent และจุดย้อนกลับ (undo checkpoint)
- `editor/agents.py` ตัวเชื่อม OpenAI / Anthropic / webhook / CLI และลูป tool-calling
- `editor/views.py`, `editor/urls.py` JSON API (แชทส่งแบบสตรีม NDJSON)
- `editor/templates`, `editor/static` หน้าจอผู้ใช้ (UI)
