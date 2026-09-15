import os
import json
import time
import re
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from jobspy import scrape_jobs

# ==========================================
# 1. SEARCH STREAMS & PARAMETERS
# ==========================================
SEARCH_TASKS = [
    # Visa Sponsorship Stream
    {"query": "visa sponsorship consulting", "loc": "United Kingdom"},
    {"query": "visa sponsorship strategy", "loc": "Singapore"},
    {"query": "tier 2 visa operations", "loc": "United Kingdom"},
    {"query": "visa sponsor associate", "loc": "Dubai, UAE"},
    {"query": "visa sponsorship consulting", "loc": "United States"},
    
    # Remote Paid Internship Stream
    {"query": "remote strategy intern", "loc": "Remote"},
    {"query": "remote operations internship", "loc": "United States"},
    {"query": "remote consulting intern", "loc": "United Kingdom"},
    
    # Direct Undisclosed / Early-Career Stream
    {"query": "strategy associate", "loc": "Dubai, UAE"},
    {"query": "junior consultant", "loc": "Singapore"},
    {"query": "operations associate", "loc": "United States"},
    {"query": "management trainee", "loc": "United Kingdom"}
]

COLUMNS_JOBS = [
    "Date Found", "Job Title", "Company", "Location", "Compensation",
    "Portfolio Match (%)", "Strategic Match Rationale", "Visa Status",
    "Application Link", "Hiring Lead Search Query"
]

COLUMNS_LEADS = [
    "Date Logged", "Target Company", "Practice Domain", "Target Persona / Recruiter",
    "Extracted Real Email (From JD)", "Estimated Corporate Pattern", 
    "Direct LinkedIn X-Ray URL", "Operational Hook Vector", "Outreach Status"
]

BANNED_TITLE_KEYWORDS = [
    "vp", "vice president", "chief", "director", "head of", "principal", 
    "lead", "senior manager", "sr. manager", "managing consultant", 
    "partner", "general manager", "executive", "architect",
    "analyst", "engineer", "engineering", "developer"
]

EXPERIENCE_OVERQUALIFIED = [
    "4+ years", "5+ years", "6+ years", "7+ years", "8+ years", 
    "10+ years", "5-7 years", "7-10 years", "minimum 5 years"
]

CORPORATE_EMAIL_SYNTAX = {
    "mckinsey": "firstname_lastname@mckinsey.com",
    "bcg": "lastname.firstname@bcg.com",
    "bain": "firstname.lastname@bain.com",
    "strategy&": "firstname.lastname@pwc.com",
    "pwc": "firstname.lastname@pwc.com",
    "ey": "firstname.lastname@ey.com",
    "deloitte": "firstnamelastname@deloitte.com",
    "kpmg": "firstnamelastname@kpmg.com",
    "amazon": "alias@amazon.com",
    "accenture": "firstname.lastname@accenture.com"
}

GENERIC_EMAILS = ['info@', 'hr@', 'careers@', 'admin@', 'support@', 'jobs@', 'apply@', 'contact@']

# ==========================================
# 2. JOB HEURISTICS & BUCKET ROUTER
# ==========================================
def analyze_and_route_job(title, description, job_type, location, source_query):
    title_lower = title.lower()
    text_lower = f"{title_lower} {description.lower()} {source_query.lower()}"
    
    if any(banned in title_lower for banned in BANNED_TITLE_KEYWORDS):
        return 0, "Disqualified: Banned Domain/Rank", False, "No", None
    
    if ("senior" in title_lower or " sr " in f" {title_lower} " or "sr." in title_lower) and "junior" not in title_lower:
        return 0, "Disqualified: Senior Title", False, "No", None

    if any(exp in text_lower for exp in EXPERIENCE_OVERQUALIFIED):
        return 0, "Disqualified: 4+ Years Required", False, "No", None

    is_intern = "intern" in text_lower or (isinstance(job_type, str) and "intern" in job_type.lower())
    is_remote = "remote" in location.lower() or "remote" in text_lower or "work from home" in text_lower
    
    is_visa = any(k in text_lower for k in [
        "visa sponsorship", "visa sponsor", "sponsorship available", 
        "relocation support", "work permit provided", "tier 2", "skilled worker visa"
    ])
    has_no_visa = any(k in text_lower for k in ["no sponsorship", "citizens only", "must have right to work", "unable to sponsor"])

    if is_visa and not has_no_visa:
        visa_status = "Sponsorship Offered"
    elif has_no_visa:
        visa_status = "No Sponsorship"
    else:
        visa_status = "Undisclosed / Verify"

    if visa_status == "Sponsorship Offered":
        assigned_bucket = "Visa_Sponsorship"
        score = 90
        rationale = "PRIORITY: Verified Visa Sponsorship"
    elif is_intern and is_remote:
        assigned_bucket = "Paid_Remote_Internships"
        score = 85
        rationale = "PRIORITY: Remote Internship Position"
    else:
        assigned_bucket = "Direct_Domestic_Undisclosed"
        score = 75
        rationale = "Direct Strategy/Ops Role (0-3Y)"

    core_hits = [k for k in ["consulting", "strategy", "operations"] if k in text_lower]
    if core_hits:
        rationale += f" | Domain: {core_hits[0].title()}"

    return score, rationale, is_intern, visa_status, assigned_bucket

# ==========================================
# 3. RECRUITER EXTRACTION & LEAD ENGINE
# ==========================================
def extract_real_recruiter_email(description):
    emails_found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', description)
    real_emails = [e for e in emails_found if not any(dummy in e.lower() for dummy in GENERIC_EMAILS)]
    return real_emails[0] if real_emails else "Pending X-Ray Outreach"

def synthesize_executive_lead(company, location, domain, description):
    company_clean = company.strip()
    company_lower = company_clean.lower()
    real_email = extract_real_recruiter_email(description)

    pattern = "firstname.lastname@company.com"
    for k, v in CORPORATE_EMAIL_SYNTAX.items():
        if k in company_lower:
            pattern = v
            break
            
    if any(tier in company_lower for tier in ["mckinsey", "bcg", "bain"]):
        target_role = "Engagement Manager / Talent Lead"
    elif any(tier in company_lower for tier in ["deloitte", "pwc", "ey", "kpmg", "accenture", "strategy&"]):
        target_role = "Practice Lead / Strategy Senior Manager"
    else:
        target_role = "Director of Strategy & Operations / Talent Partner"

    xray_query = f'site:linkedin.com/in ("{target_role.split(" / ")[0]}" OR "recruiter") "{company_clean}" "{location}"'
    xray_url = f"https://www.google.com/search?q={urllib.parse.quote(xray_query)}"

    hook_vector = "Maritime Chokepoint & Working Capital Drag" if "operations" in domain.lower() else "Macro Margin Compression & Strategy Automation"

    return {
        "company": company_clean,
        "domain": domain,
        "target_role": target_role,
        "real_email": real_email,
        "pattern": pattern,
        "xray_url": xray_url,
        "hook_vector": hook_vector
    }

# ==========================================
# 4. EMAIL DISPATCH ENGINE
# ==========================================
def send_recon_email(batch_payloads, total_new_jobs, total_new_leads):
    sender_email = os.environ.get('GMAIL_USER')
    sender_password = os.environ.get('GMAIL_APP_PASSWORD')
    recipient_email = os.environ.get('RECIPIENT_EMAIL', sender_email)

    if not sender_email or not sender_password or (total_new_jobs == 0 and total_new_leads == 0):
        print("[GHOST Email] No new positions logged or email credentials missing. Skipping dispatch.")
        return

    today_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')
    msg = MIMEMultipart()
    msg['From'] = f"GHOST Intelligence Node <{sender_email}>"
    msg['To'] = recipient_email
    msg['Subject'] = f"GHOST Recon Alert: {total_new_jobs} Early-Career Roles & {total_new_leads} Leads Logged"

    html_content = f"""
    <div style="font-family: Arial, sans-serif; color: #111; max-width: 650px; line-height: 1.5;">
        <h2 style="color: #0d1117; margin-bottom: 4px;">GHOST Autonomous Recon: 4-Hour Sync</h2>
        <p style="color: #586069; font-size: 13px; margin-top: 0;">Executed at {today_str} UTC | Target Corridors: Dubai, UK, Singapore, US, Remote</p>
        <hr style="border: 0; border-top: 1px solid #e1e4e8; margin: 16px 0;" />

        <div style="background-color: #f6f8fa; border: 1px solid #d1d5da; border-radius: 6px; padding: 12px; margin-bottom: 16px;">
            <strong style="color: #24292e;">Cycle Ingestion Summary:</strong>
            <ul style="margin: 8px 0 0 0; padding-left: 20px; font-size: 14px;">
                <li><strong>Visa Sponsorship Roles:</strong> {len(batch_payloads['Visa_Sponsorship'])}</li>
                <li><strong>Remote Paid Internships:</strong> {len(batch_payloads['Paid_Remote_Internships'])}</li>
                <li><strong>Direct Strategy/Ops Roles:</strong> {len(batch_payloads['Direct_Domestic_Undisclosed'])}</li>
                <li><strong>Executive & Recruiter Leads:</strong> {len(batch_payloads['Recruiter_Leads'])}</li>
            </ul>
        </div>
    """

    # Highlight top jobs if available
    all_jobs = batch_payloads['Visa_Sponsorship'] + batch_payloads['Paid_Remote_Internships'] + batch_payloads['Direct_Domestic_Undisclosed']
    if all_jobs:
        html_content += """
        <h3 style="color: #0366d6; margin-bottom: 8px;">Top Ingested Opportunities</h3>
        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <tr style="background-color: #eaecef; text-align: left;">
                <th style="padding: 6px; border: 1px solid #d1d5da;">Job Title</th>
                <th style="padding: 6px; border: 1px solid #d1d5da;">Company</th>
                <th style="padding: 6px; border: 1px solid #d1d5da;">Location</th>
                <th style="padding: 6px; border: 1px solid #d1d5da;">Visa Status</th>
            </tr>
        """
        for job in all_jobs[:6]:
            html_content += f"""
            <tr>
                <td style="padding: 6px; border: 1px solid #d1d5da;"><a href="{job[8]}" style="color: #0366d6; text-decoration: none;"><b>{job[1]}</b></a></td>
                <td style="padding: 6px; border: 1px solid #d1d5da;">{job[2]}</td>
                <td style="padding: 6px; border: 1px solid #d1d5da;">{job[3]}</td>
                <td style="padding: 6px; border: 1px solid #d1d5da;">{job[7]}</td>
            </tr>
            """
        html_content += "</table>"

    if batch_payloads['Recruiter_Leads']:
        html_content += """
        <h3 style="color: #28a745; margin-top: 20px; margin-bottom: 8px;">Captured Recruiter & Executive Leads</h3>
        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <tr style="background-color: #eaecef; text-align: left;">
                <th style="padding: 6px; border: 1px solid #d1d5da;">Target Firm</th>
                <th style="padding: 6px; border: 1px solid #d1d5da;">Target Persona</th>
                <th style="padding: 6px; border: 1px solid #d1d5da;">Contact Email / Pattern</th>
            </tr>
        """
        for lead in batch_payloads['Recruiter_Leads']:
            contact = lead[4] if lead[4] != "Pending X-Ray Outreach" else lead[5]
            html_content += f"""
            <tr>
                <td style="padding: 6px; border: 1px solid #d1d5da;"><b>{lead[1]}</b> ({lead[2]})</td>
                <td style="padding: 6px; border: 1px solid #d1d5da;">{lead[3]}</td>
                <td style="padding: 6px; border: 1px solid #d1d5da;"><code>{contact}</code></td>
            </tr>
            """
        html_content += "</table>"

    html_content += """
        <p style="font-size: 12px; color: #586069; margin-top: 24px;">All records have been synchronized and auto-formatted in your HOLO_EARTH Google Sheet database.</p>
    </div>
    """

    msg.attach(MIMEText(html_content, 'html'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        print(f"[GHOST Email] Alert successfully sent to {recipient_email}")
    except Exception as e:
        print(f"[GHOST Email] Dispatch failed: {e}")

# ==========================================
# 5. MAIN EXECUTION ROUTINE
# ==========================================
def main():
    print("[GHOST Recon] Initializing 4-Hour Intelligence Engine...")
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(os.environ['SPREADSHEET_ID'])

    tabs_config = {
        "Visa_Sponsorship": COLUMNS_JOBS,
        "Direct_Domestic_Undisclosed": COLUMNS_JOBS,
        "Paid_Remote_Internships": COLUMNS_JOBS,
        "Recruiter_Leads": COLUMNS_LEADS
    }

    worksheets = {}
    for tab_name, headers in tabs_config.items():
        try:
            ws = sheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = sheet.add_worksheet(title=tab_name, rows="1000", cols=str(len(headers)))
            ws.append_row(headers)
            time.sleep(1)
        worksheets[tab_name] = ws

    seen_jobs = set(worksheets["Visa_Sponsorship"].col_values(9) + 
                    worksheets["Direct_Domestic_Undisclosed"].col_values(9) + 
                    worksheets["Paid_Remote_Internships"].col_values(9))
    seen_companies = set(worksheets["Recruiter_Leads"].col_values(2))

    batch_payloads = {
        "Visa_Sponsorship": [],
        "Direct_Domestic_Undisclosed": [],
        "Paid_Remote_Internships": [],
        "Recruiter_Leads": []
    }

    # Execute Searches
    for task in SEARCH_TASKS:
        query = task["query"]
        loc = task["loc"]
        try:
            print(f"[GHOST] Scanning: '{query}' in '{loc}'...")
            jobs_df = scrape_jobs(
                site_name=["linkedin", "indeed"],
                search_term=query,
                location=loc,
                results_wanted=15,
                hours_old=72,
                country_indeed='worldwide'
            )
            
            if jobs_df.empty:
                continue

            for _, row in jobs_df.iterrows():
                title = str(row.get('title', ''))
                desc = str(row.get('description', ''))
                company = str(row.get('company', ''))
                link = str(row.get('job_url', ''))
                location_val = str(row.get('location', loc))

                if link in seen_jobs or not title or not company or company.lower() == "nan":
                    continue

                score, rationale, is_intern, visa, bucket = analyze_and_route_job(
                    title, desc, row.get('job_type', ''), location_val, query
                )
                
                if not bucket or score < 60:
                    continue

                job_record = [
                    today_str, title, company, location_val, str(row.get('salary', 'N/A')),
                    f"{score}%", rationale, visa, link,
                    f'site:linkedin.com/in "{company}" ("recruiter" OR "talent acquisition") "{location_val}"'
                ]
                batch_payloads[bucket].append(job_record)
                seen_jobs.add(link)

                # Capture up to 5 Recruiter Leads
                if company not in seen_companies and len(batch_payloads["Recruiter_Leads"]) < 5:
                    domain_match = "Strategy & Consulting"
                    for d in ["Operations", "Strategy", "Consulting"]:
                        if d.lower() in title.lower() or d.lower() in desc.lower():
                            domain_match = d
                            break

                    lead = synthesize_executive_lead(company, location_val, domain_match, desc)
                    batch_payloads["Recruiter_Leads"].append([
                        today_str, lead['company'], lead['domain'],
                        lead['target_role'], lead['real_email'], lead['pattern'],
                        lead['xray_url'], lead['hook_vector'], "Queued"
                    ])
                    seen_companies.add(company)

        except Exception as e:
            print(f"[GHOST] Notice for '{query}': {e}")
            time.sleep(1.5)

    total_jobs_added = sum(len(batch_payloads[k]) for k in ["Visa_Sponsorship", "Direct_Domestic_Undisclosed", "Paid_Remote_Internships"])
    total_leads_added = len(batch_payloads["Recruiter_Leads"])

    # Commit Batches
    for tab_name, rows in batch_payloads.items():
        if rows:
            ws = worksheets[tab_name]
            ws.append_rows(rows, value_input_option='USER_ENTERED')
            print(f"[GHOST] Wrote {len(rows)} rows to '{tab_name}'.")

    # Auto-Format All 4 Worksheets (Wrap, Top Align, Freeze Header, Auto-Resize)
    for tab_name, ws in worksheets.items():
        try:
            sheet.batch_update({
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": ws.id,
                                "gridProperties": {"frozenRowCount": 1}
                            },
                            "fields": "gridProperties.frozenRowCount"
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {"sheetId": ws.id},
                            "cell": {
                                "userEnteredFormat": {
                                    "wrapStrategy": "WRAP",
                                    "verticalAlignment": "TOP"
                                }
                            },
                            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"
                        }
                    },
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": ws.id,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": len(tabs_config[tab_name])
                            }
                        }
                    }
                ]
            })
            time.sleep(1)
        except Exception as e:
            print(f"[GHOST] Formatting notice for {tab_name}: {e}")

    # Dispatch Email Briefing
    send_recon_email(batch_payloads, total_jobs_added, total_leads_added)

    print("[GHOST Recon] Pipeline Execution Complete.")

if __name__ == "__main__":
    main()
    
