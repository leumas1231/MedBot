import discord
import re
import gspread
import os
import json
from dotenv import load_dotenv
load_dotenv()
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from collections import defaultdict

# ================= CONFIG =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1439473833273856120                   # text channel if needed
SPREADSHEET_ID = "1aXhvKbXqXlHEu94dQctSJP8jk6tLvNWkrYHZyDYcI0c"
GUILD_ID = 861362652710174740                   # your real server (guild) ID

# Google Sheets auth
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Instead of GOOGLE_CREDENTIALS
CREDS = Credentials.from_service_account_file(
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
    scopes=SCOPES
)

GC = gspread.authorize(CREDS)
SHEET = GC.open_by_key(SPREADSHEET_ID).sheet1  # first worksheet with raw logs

# Expected header row in the first sheet:
# Timestamp | Medics | Job Name | Duration | Points | Clients | Participant Names | Description | Report Date | Message Link


# ================= NAME NORMALIZATION =================
def load_medic_normalization():
    """Reads all medic names from the sheet & builds a normalization map."""
    records = SHEET.get_all_records()
    mapping = {}

    for row in records:
        medics = row.get("Medics", "")
        for m in [x.strip() for x in medics.split(",") if x.strip()]:
            key = m.lower()
            if key not in mapping:
                mapping[key] = m  # store original capitalization
    return mapping


def normalize_medic_name(name: str, mapping: dict) -> str:
    """Converts a medic name to correct capitalization."""
    key = name.lower()
    if key in mapping:
        return mapping[key]  # already known medic → use canonical case
    else:
        # New medic never seen before → Title Case
        proper = name.title()
        mapping[key] = proper
        return proper


# ================= POINT CALCULATOR =================
def calculate_points(job_name: str, duration: int, clients: int) -> int:
    job_name = job_name.lower().strip()

    # Hosted Event — 30 points, must be at least 60 min and 5+ clients
    if "hosted event" in job_name:
        if duration >= 60 and clients >= 5:
            return 30
        return 0

    if "raid" in job_name or "defend" in job_name:
        return 3 + 2 * (duration // 15)
    if "criminal" in job_name or "lmpf" in job_name:
        return 3
    if "healing" in job_name or "lowbie" in job_name or "farm" in job_name:
        return clients + (duration // 15)
    if "rev" in job_name or "spar" in job_name:
        return clients + (duration // 15)
    if "escort" in job_name:
        return 2
    if "boss" in job_name or "world" in job_name:
        return clients * 3
    if "arc" in job_name:
        return clients * 30
    if "mission" in job_name or "daily" in job_name:
        return clients * 3

    return 0


# ================= RANK BONUS (BASED ON MANUAL RANK) =================
def bonus_from_rank(rank: str) -> float:
    """Return bonus multiplier based on Rank string from Master Log."""
    r = (rank or "").lower()
    if "doctor" in r:
        return 3.0
    if "paramedic" in r:
        return 2.0
    if "senior" in r:
        return 1.5
    if "junior" in r:
        return 1.25
    if "field" in r:
        return 1.15
    return 1.0  # Unranked / unknown


# ================= MONTHLY LEADERBOARD =================
def update_leaderboard():
    records = SHEET.get_all_records()
    now = datetime.now()
    current_month = now.month
    current_year = now.year
    current_month_name = now.strftime("%b")

    sheet_title = f"Leaderboard - {current_month_name} {current_year}"

    ss = GC.open_by_key(SPREADSHEET_ID)

    # Load ranks from Master Log (if exists)
    rank_by_medic = {}
    try:
        master = ss.worksheet("Leaf Master Medical Log")
        master_records = master.get_all_records()
        for row in master_records:
            medic_name = row.get("Medic", "").strip()
            if medic_name:
                rank_by_medic[medic_name] = row.get("Rank", "Unranked")
    except gspread.exceptions.WorksheetNotFound:
        # No master sheet yet; everyone effectively Unranked
        rank_by_medic = {}

    # Create or open the monthly leaderboard sheet
t# Create or open the monthly leaderboard sheet
try:
    leaderboard_sheet = ss.worksheet(sheet_title)
except gspread.exceptions.WorksheetNotFound:
    leaderboard_sheet = ss.add_worksheet(title=sheet_title, rows=200, cols=10)
    leaderboard_sheet.update([[
        "Rank", "Medic", "Raw Points", "Jobs Logged",
        "Rank Title", "Bonus Multiplier",
        "Adjusted Points", "Total Pay", "Total Ryo"
    ]])
    leaderboard_sheet.update("I2", [[5000]])  # ONLY set default for new sheets


    # Load BANK_RYO from cell I2
    try:
        BANK_RYO = float(leaderboard_sheet.acell("I2").value)
    except:
        BANK_RYO = 5000  # fallback

    points_by_medic = defaultdict(int)
    jobs_by_medic = defaultdict(int)

    for row in records:
        date_str = str(row.get("Report Date", "")).strip()
        if not date_str:
            continue

        try:
            d = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            continue

        if d.month == current_month and d.year == current_year:
            medics_raw = row.get("Medics", "")
            try:
                points = int(row.get("Points", 0))
            except ValueError:
                points = 0

            for medic in [m.strip() for m in medics_raw.split(",") if m.strip()]:
                points_by_medic[medic] += points
                jobs_by_medic[medic] += 1

    if not points_by_medic:
        leaderboard_sheet.clear()
        leaderboard_sheet.update([["No data for this month."]])
        return [], {}

    # Adjust with rank bonuses (from Master Log Rank)
    adjusted_points = {}
    for medic, raw in points_by_medic.items():
        rank = rank_by_medic.get(medic, "Unranked")
        mult = bonus_from_rank(rank)
        adjusted_points[medic] = raw * mult

    total_adjusted = sum(adjusted_points.values())
    sorted_data = sorted(adjusted_points.items(), key=lambda x: x[1], reverse=True)

    output = [[
        "Rank", "Medic", "Raw Points", "Jobs Logged",
        "Rank Title", "Bonus Multiplier",
        "Adjusted Points", "Total Pay", "Total Ryo"
    ]]

    for i, (medic, adj) in enumerate(sorted_data, start=1):
        raw = points_by_medic[medic]
        jobs = jobs_by_medic[medic]
        rank_title = rank_by_medic.get(medic, "Unranked")
        mult = bonus_from_rank(rank_title)
        share = adj / total_adjusted if total_adjusted > 0 else 0
        pay = round(share * BANK_RYO, 2)

        output.append([
            i,
            medic,
            raw,
            jobs,
            rank_title,
            mult,
            round(adj, 2),
            pay,
            BANK_RYO if i == 1 else ""
        ])

    leaderboard_sheet.clear()
    leaderboard_sheet.update(output)

    print(f"✅ Leaderboard updated for {current_month_name} {current_year}")
    return sorted_data, jobs_by_medic

def update_single_leaderboard(year: int, month: int):
    ss = GC.open_by_key(SPREADSHEET_ID)
    records = SHEET.get_all_records()

    sheet_title = f"Leaderboard - {datetime(year, month, 1).strftime('%b')} {year}"

    # Load ranks
    try:
        master = ss.worksheet("Leaf Master Medical Log")
        master_records = master.get_all_records()
        rank_by_medic = {
            row.get("Medic", ""): row.get("Rank", "Unranked")
            for row in master_records
        }
    except gspread.exceptions.WorksheetNotFound:
        rank_by_medic = {}

    # Create or open the sheet
# Create or open the monthly leaderboard sheet
try:
    leaderboard_sheet = ss.worksheet(sheet_title)
except gspread.exceptions.WorksheetNotFound:
    leaderboard_sheet = ss.add_worksheet(title=sheet_title, rows=200, cols=10)
    leaderboard_sheet.update([[
        "Rank", "Medic", "Raw Points", "Jobs Logged",
        "Rank Title", "Bonus Multiplier",
        "Adjusted Points", "Total Pay", "Total Ryo"
    ]])
    leaderboard_sheet.update("I2", [[5000]])  # ONLY set default for new sheets


    # Load BANK_RYO from sheet cell I2
    try:
        BANK_RYO = float(leaderboard_sheet.acell("I2").value)
    except:
        BANK_RYO = 5000

    # Collect raw data for this month
    points_by_medic = defaultdict(int)
    jobs_by_medic = defaultdict(int)

    for row in records:
        date_str = str(row.get("Report Date", "")).strip()
        if not date_str:
            continue

        try:
            d = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            continue

        if d.year == year and d.month == month:
            medics = [m.strip() for m in row.get("Medics", "").split(",") if m.strip()]
            pts = int(row.get("Points", 0))

            for medic in medics:
                points_by_medic[medic] += pts
                jobs_by_medic[medic] += 1

    # If empty month
    if not points_by_medic:
        leaderboard_sheet.update([["No data for this month."]])
        return

    adjusted = {}
    for medic, raw_pts in points_by_medic.items():
        rank = rank_by_medic.get(medic, "Unranked")
        mult = bonus_from_rank(rank)
        adjusted[medic] = raw_pts * mult

    total_adj = sum(adjusted.values())

    output = [[
        "Rank", "Medic", "Raw Points", "Jobs Logged",
        "Rank Title", "Bonus Multiplier",
        "Adjusted Points", "Total Pay", "Total Ryo"
    ]]

    sorted_medics = sorted(adjusted.items(), key=lambda x: x[1], reverse=True)

    for i, (medic, adj_pts) in enumerate(sorted_medics, start=1):
        raw_pts = points_by_medic[medic]
        jobs = jobs_by_medic[medic]
        rank_title = rank_by_medic.get(medic, "Unranked")
        mult = bonus_from_rank(rank_title)
        share = adj_pts / total_adj if total_adj else 0
        pay = round(share * BANK_RYO, 2)

        output.append([
            i, medic, raw_pts, jobs, rank_title, mult,
            round(adj_pts, 2), pay,
            BANK_RYO if i == 1 else ""
        ])

    leaderboard_sheet.clear()
    leaderboard_sheet.update(output)

    print(f"Updated leaderboard: {sheet_title}")


def update_all_leaderboards():
    """Rebuild leaderboard sheets for every month found in the raw log."""
    ss = GC.open_by_key(SPREADSHEET_ID)
    records = SHEET.get_all_records()

    # Find all months with data
    months = set()

    for row in records:
        date_str = str(row.get("Report Date", "")).strip()
        if not date_str:
            continue

        try:
            d = datetime.strptime(date_str, "%m/%d/%Y")
            months.add((d.year, d.month))
        except ValueError:
            continue

    # Sort oldest → newest
    months = sorted(months)

    # Rebuild the leaderboard for each month
    for year, month in months:
        month_name = datetime(year, month, 1).strftime("%b")
        title = f"Leaderboard - {month_name} {year}"

        # Temporarily override datetime.now() behavior
        print(f"📅 Updating leaderboard for: {title}")
        update_single_leaderboard(year, month)


# ================= MASTER LOG (LIFETIME) =================
def update_master_log():
    ss = GC.open_by_key(SPREADSHEET_ID)

    # Ensure master sheet exists & capture existing ranks
    try:
        master = ss.worksheet("Leaf Master Medical Log")
        existing_records = master.get_all_records()
        existing_ranks = {
            row.get("Medic", "").strip(): row.get("Rank", "Unranked")
            for row in existing_records
            if row.get("Medic", "").strip()
        }
    except gspread.exceptions.WorksheetNotFound:
        master = ss.add_worksheet(
            title="Leaf Master Medical Log", rows="300", cols="20"
        )
        existing_ranks = {}
        master.update([[
            "Medic", "Rank", "Total Jobs", "Total Raw Points",
            "Total Adjusted Points", "Total Hours", "Raid Hours",
            "LMPF Hours", "Healing Hours", "Rev/Spar Hours",
            "Escort Hours", "World Boss Hours", "Arc Hours",
            "Mission Hours", "Hosted Event Hours"
        ]])

    records = SHEET.get_all_records()

    raw_points = defaultdict(int)
    jobs = defaultdict(int)
    hours = defaultdict(float)
    hours_by_type = defaultdict(lambda: defaultdict(float))

    for row in records:
        medics_raw = row.get("Medics", "")
        job_name = str(row.get("Job Name", "")).lower()
        try:
            points = int(row.get("Points", 0))
        except ValueError:
            points = 0

        # Duration like "45 min"
        duration_str = str(row.get("Duration", "0 min"))
        try:
            minutes = int(duration_str.split()[0])
        except (ValueError, IndexError):
            minutes = 0
        job_hours = minutes / 60.0

        medics = [m.strip() for m in medics_raw.split(",") if m.strip()]
        for medic in medics:
            raw_points[medic] += points
            jobs[medic] += 1
            hours[medic] += job_hours

            if "raid" in job_name or "defend" in job_name:
                hours_by_type[medic]["Raid"] += job_hours
            elif "lmpf" in job_name:
                hours_by_type[medic]["LMPF"] += job_hours
            elif "healing" in job_name or "lowbie" in job_name:
                hours_by_type[medic]["Healing"] += job_hours
            elif "rev" in job_name or "spar" in job_name:
                hours_by_type[medic]["Rev/Spar"] += job_hours
            elif "escort" in job_name:
                hours_by_type[medic]["Escort"] += job_hours
            elif "world" in job_name:
                hours_by_type[medic]["World Boss"] += job_hours
            elif "arc" in job_name:
                hours_by_type[medic]["Arc"] += job_hours
            elif "mission" in job_name:
                hours_by_type[medic]["Mission"] += job_hours
            elif "hosted event" in job_name:
                hours_by_type[medic]["Hosted Event"] += job_hours

    output = [[
        "Medic", "Rank", "Total Jobs", "Total Raw Points",
        "Total Adjusted Points", "Total Hours", "Raid Hours",
        "LMPF Hours", "Healing Hours", "Rev/Spar Hours",
        "Escort Hours", "World Boss Hours", "Arc Hours",
        "Mission Hours", "Hosted Event Hours"
    ]]

    for medic in sorted(jobs.keys()):
        rank = existing_ranks.get(medic, "Unranked")
        bonus_mult = bonus_from_rank(rank)
        adjusted = raw_points[medic] * bonus_mult

        output.append([
            medic,
            rank,
            jobs[medic],
            raw_points[medic],
            adjusted,
            round(hours[medic], 2),
            round(hours_by_type[medic]["Raid"], 2),
            round(hours_by_type[medic]["LMPF"], 2),
            round(hours_by_type[medic]["Healing"], 2),
            round(hours_by_type[medic]["Rev/Spar"], 2),
            round(hours_by_type[medic]["Escort"], 2),
            round(hours_by_type[medic]["World Boss"], 2),
            round(hours_by_type[medic]["Arc"], 2),
            round(hours_by_type[medic]["Mission"], 2),
            round(hours_by_type[medic]["Hosted Event"], 2),
        ])

    master.clear()
    master.update(output)
    print("✅ Leaf Master Medical Log updated")


# ================= DISCORD BOT =================
intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)


# ================= Update ALL leaderboards =================
@tree.command(name="updatelogs", description="Force update ALL leaderboard sheets and the master log.")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def update_logs(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        update_master_log()
        update_all_leaderboards()
        await interaction.followup.send("✅ All logs and leaderboards updated!")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error: {e}")

# ---------- /leaderboard (monthly) ----------
@tree.command(name="leaderboard", description="Show this month's medic leaderboard")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def leaderboard_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    try:
        sorted_data, jobs_by_medic = update_leaderboard()

        if not sorted_data:
            await interaction.followup.send("📋 No medic data found for this month.")
            return

        lines = []
        for i, (medic, points) in enumerate(sorted_data[:10], start=1):
            job_count = jobs_by_medic.get(medic, 0)
            lines.append(f"**{i}. {medic}** — {points} pts ({job_count} jobs)")

        leaderboard_text = "\n".join(lines)
        now = datetime.now()

        embed = discord.Embed(
            title=f"🏆 Medic Leaderboard — {now.strftime('%B %Y')}",
            description=leaderboard_text,
            color=0xFFD700,
        )
        embed.set_footer(text="Data pulled from Google Sheets")

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error loading leaderboard: {e}")

# ---------- /medicstats (lifetime) ----------
@tree.command(name="medicstats", description="View lifetime stats for a specific medic")
@discord.app_commands.describe(name="The medic's name")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def medicstats(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=False)

    try:
        master = GC.open_by_key(SPREADSHEET_ID).worksheet("Leaf Master Medical Log")
        records = master.get_all_records()

        if not records:
            await interaction.followup.send("⚠️ No lifetime data found.")
            return

        target = None
        for row in records:
            medic_name = row.get("Medic", "")
            if name.lower() in medic_name.lower():
                target = row
                break

        if not target:
            await interaction.followup.send(f"❌ No medic found matching: **{name}**")
            return

        medic = target.get("Medic", "Unknown")
        rank = target.get("Rank", "Unranked")
        jobs = target.get("Total Jobs", 0)
        raw = target.get("Total Raw Points", 0)
        adj = target.get("Total Adjusted Points", 0)
        hours = target.get("Total Hours", 0)

        raid_h = target.get("Raid Hours", 0)
        lmpf_h = target.get("LMPF Hours", 0)
        heal_h = target.get("Healing Hours", 0)
        rev_h = target.get("Rev/Spar Hours", 0)
        escort_h = target.get("Escort Hours", 0)
        boss_h = target.get("World Boss Hours", 0)
        arc_h = target.get("Arc Hours", 0)
        mission_h = target.get("Mission Hours", 0)
        event_h = target.get("Hosted Event Hours", 0)

        embed = discord.Embed(
            title=f"💠 Lifetime Stats — {medic}",
            color=0x3498DB,
        )

        embed.add_field(name="Rank", value=rank, inline=True)
        embed.add_field(name="Total Jobs", value=jobs, inline=True)
        embed.add_field(name="Total Raw Points", value=raw, inline=True)
        embed.add_field(name="Total Adjusted Points", value=adj, inline=True)
        embed.add_field(name="Total Hours", value=hours, inline=True)

        embed.add_field(
            name="Hours Breakdown",
            value=(
                f"• **Raid:** {raid_h}\n"
                f"• **LMPF:** {lmpf_h}\n"
                f"• **Healing:** {heal_h}\n"
                f"• **Rev/Spar:** {rev_h}\n"
                f"• **Escort:** {escort_h}\n"
                f"• **World Boss:** {boss_h}\n"
                f"• **Arc:** {arc_h}\n"
                f"• **Mission:** {mission_h}\n"
                f"• **Hosted Event:** {event_h}"
            ),
            inline=False,
        )

        embed.set_footer(text="Lifetime stats from the Master Medical Log")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error: {e}")


# ---------- /report ----------
@tree.command(name="report", description="Submit a medic report")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def report(interaction: discord.Interaction):

    class JobSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label="Raid / Defend", value="Raid / Defend"),
                discord.SelectOption(label="Duty with LMPF", value="LMPF"),
                discord.SelectOption(label="Healing Lowbies", value="Healing Lowbies"),
                discord.SelectOption(label="Rev Spar", value="Rev Spar"),
                discord.SelectOption(label="Escort", value="Escort"),
                discord.SelectOption(label="World Boss", value="World Boss"),
                discord.SelectOption(label="Arc", value="Arc"),
                discord.SelectOption(label="Mission", value="Daily Mission"),
                discord.SelectOption(label="Hosted Event", value="Hosted Event"),
            ]
            super().__init__(placeholder="Choose Job Type...", options=options)

        async def callback(self, select_interaction: discord.Interaction):
            job_type = self.values[0]

            class ReportModal(discord.ui.Modal, title="Medic Job Report"):
                medics = discord.ui.TextInput(label="Medic Names(Separate by ,)", placeholder="Example: Leumas, LeaKiara, Ragnor Reaper")
                date = discord.ui.TextInput(label="Date (blank = today, MM/DD/YYYY)", required=False, placeholder="01/15/2025")
                time_range = discord.ui.TextInput(label="Time Range (HH:MM or H:MM AM/PM)", placeholder="5:00 pm - 6:00 pm")
                clients = discord.ui.TextInput(label="Clients(Separate by ,)", placeholder="Example: Leumas, LeaKiara, Ragnor Reaper")
                description = discord.ui.TextInput(
                    label="Description", style=discord.TextStyle.long
                )

                def parse_time(self, t):
                    for fmt in ("%H:%M", "%I:%M %p"):
                        try:
                            return datetime.strptime(t.strip(), fmt)
                        except ValueError:
                            pass
                    return None

                def parse_date(self, d):
                    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                        try:
                            return datetime.strptime(d.strip(), fmt).date()
                        except ValueError:
                            pass
                    return None

                async def on_submit(self, modal_interaction: discord.Interaction):
                    try:
                        await modal_interaction.response.defer(ephemeral=True)

                        # Load normalization table and normalize medic names
                        name_map = load_medic_normalization()
                        medic_list = [
                            normalize_medic_name(m.strip(), name_map)
                            for m in re.split(r",|\band\b", self.medics.value)
                            if m.strip()
                        ]

                        clients_list = [
                            p.strip()
                            for p in re.split(r",|\band\b", self.clients.value)
                            if p.strip()
                        ]

                        date_obj = (
                            self.parse_date(self.date.value)
                            if self.date.value.strip()
                            else datetime.now().date()
                        )

                        # Parse time range
                        t = re.split(r"-|to", self.time_range.value)
                        start = self.parse_time(t[0])
                        end = self.parse_time(t[1])

                        if not start or not end:
                            await modal_interaction.followup.send(
                                "⚠️ Invalid time format. Use `HH:MM` or `H:MM AM/PM` with `-` or `to`.",
                                ephemeral=True,
                            )
                            return

                        start_dt = datetime.combine(date_obj, start.time())
                        end_dt = datetime.combine(date_obj, end.time())
                        if end_dt < start_dt:
                            end_dt += timedelta(days=1)

                        duration = int((end_dt - start_dt).total_seconds() // 60)
                        points = calculate_points(job_type, duration, len(clients_list))
                        desc = self.description.value.strip()

                        embed = discord.Embed(
                            title=f"Medic Report — {job_type}",
                            description=desc,
                            color=0x00FFAA,
                        )
                        embed.add_field(name="Date", value=date_obj.strftime("%B %d, %Y"))
                        embed.add_field(
                            name="Medics", value=", ".join(medic_list), inline=False
                        )
                        embed.add_field(name="Duration", value=f"{duration} min")
                        embed.add_field(
                            name="Clients", value=str(len(clients_list))
                        )
                        embed.add_field(name="Points", value=str(points))
                        embed.timestamp = datetime.now()

                        msg = await modal_interaction.channel.send(embed=embed)

                        link = f"https://discord.com/channels/{modal_interaction.guild.id}/{modal_interaction.channel.id}/{msg.id}"
                        hyperlink = f'=HYPERLINK("{link}", "View Report")'

                        SHEET.append_row(
                            [
                                datetime.now().strftime("%m/%d/%Y %H:%M"),
                                ", ".join(medic_list),
                                job_type,
                                f"{duration} min",
                                points,
                                len(clients_list),
                                ", ".join(clients_list),
                                desc,
                                date_obj.strftime("%m/%d/%Y"),
                                hyperlink,
                            ],
                            value_input_option="USER_ENTERED",
                        )

                        # Update monthly leaderboard & master log
                        update_master_log()
                        update_leaderboard()

                        await modal_interaction.followup.send(
                            "✅ Report logged and all sheets updated!",
                            ephemeral=True,
                        )

                    except Exception as e:
                        await modal_interaction.followup.send(
                            f"⚠️ Error: {e}",
                            ephemeral=True,
                        )

            await select_interaction.response.send_modal(ReportModal())

    class JobSelectView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(JobSelect())

    await interaction.response.send_message(
        "Choose your **Job Type** to begin your report:",
        view=JobSelectView(),
        ephemeral=True,
    )


@bot.event
async def on_ready():
    synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
    print(f"Logged in as {bot.user}")


bot.run(DISCORD_TOKEN)
