import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
from datetime import datetime, timedelta
import pytz

tz = pytz.timezone('Asia/Bangkok')

class TaskBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True 
        super().__init__(command_prefix="!", intents=intents)

    # 1. นิยามฟังก์ชันแจ้งเตือนไว้ข้างบนสุดเพื่อให้บอทรู้จักก่อนเริ่มงาน
    @tasks.loop(hours=12)
    async def check_deadline(self):
        now = datetime.now(tz)
        tomorrow = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, title, assignee_id FROM tasks WHERE deadline = ? AND status != 'เสร็จ'", (tomorrow,))
        for tid, title, uid in c.fetchall():
            user = await self.fetch_user(uid)
            if user: 
                try: await user.send(f"⚠️ **เดดไลน์พรุ่งนี้!** งาน `{tid}: {title}`")
                except: pass
        conn.close()

    async def setup_hook(self):
        # สร้าง Database
        conn = sqlite3.connect('tasks.db')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS tasks
                     (id TEXT PRIMARY KEY, title TEXT, po_id INTEGER, 
                      assignee_id INTEGER, deadline TEXT, status TEXT, dependent_on TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings
                     (key TEXT PRIMARY KEY, value INTEGER)''')
        conn.commit()
        conn.close()
        
        # เริ่มการแจ้งเตือน
        self.check_deadline.start()

    async def on_ready(self):
        print(f'✅ บอทออนไลน์แล้ว: {self.user}')
        await self.tree.sync()

bot = TaskBot()

# --- Helpers ---
def get_db():
    return sqlite3.connect('tasks.db')

def get_main_po():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'main_po'")
    res = c.fetchone()
    conn.close()
    return res[0] if res else None

def generate_id():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1")
    res = c.fetchone()
    conn.close()
    if res:
        num = int(res[0][2:]) + 1
        return f"KT{num:03d}"
    return "KT001"

# --- UI Views (Permission & State Lock) ---
class TaskControlView(discord.ui.View):
    def __init__(self, task_id):
        super().__init__(timeout=None)
        self.task_id = task_id

    @discord.ui.button(label="รับทราบงาน", style=discord.ButtonStyle.blurple)
    async def acknowledge(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await self.update_status_logic(interaction, "รับทราบงานแล้ว", allow_from=["มอบหมายแล้ว"])

    @discord.ui.button(label="เริ่มทำ", style=discord.ButtonStyle.green)
    async def start_work(self, interaction: discord.Interaction, btn: discord.ui.Button):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT dependent_on FROM tasks WHERE id = ?", (self.task_id,))
        dep_id = c.fetchone()[0]
        if dep_id:
            c.execute("SELECT status FROM tasks WHERE id = ?", (dep_id,))
            dep_status = c.fetchone()
            if not dep_status or dep_status[0] != "เสร็จ":
                conn.close()
                return await interaction.response.send_message(f"❌ ต้องรอให้งาน `{dep_id}` เสร็จก่อน!", ephemeral=True)
        conn.close()
        await self.update_status_logic(interaction, "กำลังทำ", allow_from=["รับทราบงานแล้ว"])

    @discord.ui.button(label="เสร็จรอตรวจ", style=discord.ButtonStyle.gray)
    async def submit(self, interaction: discord.Interaction, btn: discord.ui.Button):
        success = await self.update_status_logic(interaction, "เสร็จรอตรวจ", allow_from=["กำลังทำ"])
        if success:
            main_po_id = get_main_po()
            if main_po_id:
                po = await bot.fetch_user(main_po_id)
                if po: await po.send(f"🔔 **งานส่งตรวจ:** `{self.task_id}` เสร็จแล้วครับ เชิญมาตรวจสอบด้วย")

    async def update_status_logic(self, interaction, new_status, allow_from):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT status, assignee_id FROM tasks WHERE id = ?", (self.task_id,))
        res = c.fetchone()
        if not res:
            conn.close()
            return False
        
        curr_status, assignee_id = res
        if interaction.user.id != assignee_id:
            conn.close()
            await interaction.response.send_message("❌ เฉพาะผู้รับผิดชอบงานนี้เท่านั้นที่กดได้", ephemeral=True)
            return False
        
        if curr_status not in allow_from:
            conn.close()
            await interaction.response.send_message(f"❌ ต้องทำตามลำดับสถานะ (ปัจจุบัน: {curr_status})", ephemeral=True)
            return False

        c.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, self.task_id))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"อัปเดต `{self.task_id}` เป็น **{new_status}** สำเร็จ!", ephemeral=True)
        return True

class ReviewView(discord.ui.View):
    def __init__(self, task_id, assignee_id):
        super().__init__(timeout=None)
        self.task_id, self.assignee_id = task_id, assignee_id

    @discord.ui.button(label="ผ่าน", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await self.finish_review(interaction, "เสร็จ", "✅ งานผ่านการตรวจสอบแล้ว!")

    @discord.ui.button(label="ไม่ผ่าน", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await self.finish_review(interaction, "รับทราบงานแล้ว", "❌ ตรวจไม่ผ่าน กรุณาแก้ไขใหม่")

    async def finish_review(self, interaction, status, msg):
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, self.task_id))
        conn.commit()
        conn.close()
        user = await bot.fetch_user(self.assignee_id)
        if user: await user.send(f"📢 `{self.task_id}`: {msg}")
        await interaction.response.send_message(f"บันทึกผลการตรวจ `{self.task_id}` แล้ว", ephemeral=True)

# --- Commands ---
@bot.tree.command(name="set_po", description="กำหนด PO หลัก (Admin Only)")
@app_commands.checks.has_permissions(administrator=True)
async def set_po(interaction: discord.Interaction, user: discord.Member):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('main_po', ?)", (user.id,))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ ตั้ง {user.mention} เป็น Product Owner หลัก", ephemeral=True)

@bot.tree.command(name="add_task", description="PO สั่งงานใหม่")
async def add_task(interaction: discord.Interaction, title: str, assignee: discord.Member, deadline: str, dependent_on: str = None):
    if interaction.user.id != get_main_po():
        return await interaction.response.send_message("❌ เฉพาะ PO เท่านั้นที่สั่งงานได้", ephemeral=True)
    
    tid = generate_id()
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", (tid, title, interaction.user.id, assignee.id, deadline, "มอบหมายแล้ว", dependent_on))
    conn.commit()
    conn.close()
    
    emb = discord.Embed(title=f"🆕 Task: {tid}", color=discord.Color.blue())
    emb.add_field(name="งาน", value=title).add_field(name="คนทำ", value=assignee.mention)
    await interaction.response.send_message(embed=emb, view=TaskControlView(tid))

@bot.tree.command(name="my_tasks", description="ดูงานค้างของคุณ")
async def my_tasks(interaction: discord.Interaction):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, title, status, deadline FROM tasks WHERE assignee_id = ? AND status != 'เสร็จ'", (interaction.user.id,))
    rows = c.fetchall()
    conn.close()
    if not rows: return await interaction.response.send_message("ไม่มีงานค้าง!", ephemeral=True)
    
    await interaction.response.send_message("รายการงานของคุณ:", ephemeral=True)
    for r in rows:
        emb = discord.Embed(title=f"📋 Task: {r[0]}", color=discord.Color.green())
        emb.add_field(name="หัวข้อ", value=r[1]).add_field(name="สถานะ", value=r[2])
        await interaction.followup.send(embed=emb, view=TaskControlView(r[0]), ephemeral=True)

@bot.tree.command(name="review_task", description="PO ตรวจงาน")
async def review(interaction: discord.Interaction, task_id: str):
    if interaction.user.id != get_main_po():
        return await interaction.response.send_message("❌ เฉพาะ PO เท่านั้นที่ตรวจงานได้", ephemeral=True)
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT assignee_id, title FROM tasks WHERE id = ?", (task_id,))
    res = c.fetchone()
    conn.close()
    if not res: return await interaction.response.send_message("ไม่พบงาน", ephemeral=True)
    
    await interaction.response.send_message(f"ตรวจงาน: {task_id}", view=ReviewView(task_id, res[0]), ephemeral=True)

@bot.tree.command(name="manage_task", description="PO แก้ไข/ลบงาน")
@app_commands.choices(action=[
    app_commands.Choice(name="ลบทิ้ง", value="delete"),
    app_commands.Choice(name="เปลี่ยนคนทำ", value="reassign")
])
async def manage(interaction: discord.Interaction, task_id: str, action: str, new_assignee: discord.Member = None):
    if interaction.user.id != get_main_po():
        return await interaction.response.send_message("❌ เฉพาะ PO เท่านั้นที่ใช้คำสั่งนี้ได้", ephemeral=True)
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT assignee_id, title FROM tasks WHERE id = ?", (task_id,))
    res = c.fetchone()
    if not res: 
        conn.close()
        return await interaction.response.send_message("ไม่พบงาน", ephemeral=True)
    
    old_assignee_id = res[0]
    title = res[1]

    if action == "delete":
        c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        msg = f"ลบงาน {task_id} แล้ว"
    elif action == "reassign" and new_assignee:
        c.execute("UPDATE tasks SET assignee_id = ?, status = 'มอบหมายแล้ว' WHERE id = ?", (new_assignee.id, task_id))
        msg = f"เปลี่ยนคนทำ {task_id} เป็น {new_assignee.display_name}"
        # DM แจ้งเตือน
        try: await new_assignee.send(f"📦 คุณได้รับงานใหม่ (Reassign): {task_id}")
        except: pass
        try:
            old_user = await bot.fetch_user(old_assignee_id)
            if old_user: await old_user.send(f"🔄 งาน {task_id} ถูกย้ายไปให้คนอื่นแล้ว")
        except: pass

    conn.commit()
    conn.close()
    await interaction.response.send_message(msg, ephemeral=True)

bot.run('MTQ2ODExNTA5MTM1NzU2OTA0Ng.GbQmgB.RcrsFlXNNe5HX-eYT5iMznTxoE5mOv7AH-jQY8')