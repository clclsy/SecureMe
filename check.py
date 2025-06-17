from flask import Flask, request, render_template
from serpapi.google_search import GoogleSearch
import google.generativeai as genai
import os, json, re, base64, requests
from PIL import Image
import pytesseract
from io import BytesIO
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from dotenv import load_dotenv
from pymongo import MongoClient
from difflib import SequenceMatcher
from datetime import datetime
from mongo_utils import log_scan_result, log_fbi_result, log_face_match

load_dotenv()
pytesseract.pytesseract.tesseract_cmd = r"C:\\Users\\clsy1\\AppData\\Local\\Programs\\Tesseract-OCR\\tesseract.exe"
os.makedirs("static/uploads", exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
GEMINI_KEY = os.getenv("GEMINI_KEY")
MONGO_URI = os.getenv("MONGO_URI")
OFFENDERS_IO_KEY = os.getenv("OFFENDERS_IO_KEY")
print("✅ OFFENDERS_IO_KEY loaded:", bool(OFFENDERS_IO_KEY))



client = MongoClient(MONGO_URI)
mongo_db = client["secureme"]

genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-1.5-pro")

@app.errorhandler(RequestEntityTooLarge)
def handle_413(error):
    return "❌ Image too large. Please upload a smaller file.", 413

def normalize_name(name):
    return " ".join(sorted(name.lower().split()))

def similar_names(a, b):
    return normalize_name(a) == normalize_name(b)

def is_dangerous_tag(tag):
    danger_keywords = ["toxic", "cheater", "scammer", "abusive", "controlling", "boundary", "unsafe", "manipulative"]
    return any(word in tag.lower() for word in danger_keywords)

def is_safe(tags):
    return not any(is_dangerous_tag(tag) for tag in tags)

def search_web(query):
    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_KEY,
        "num": 5
    }
    try:
        results = GoogleSearch(params).get_dict()
        return [
            {
                "title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "link": r.get("link", "")
            }
            for r in results.get("organic_results", [])
            if r.get("snippet") and r.get("link")
        ]
    except Exception as e:
        print("❌ SerpAPI search error:", e)
        return []

def get_gemini_summary(name, links):
    combined = "\n\n".join(f"{l['title']} - {l['snippet']}" for l in links)
    if not combined.strip():
        combined = name
    prompt = f"""
You are an AI assistant reviewing information pulled from a dating profile or name search.

Your job is to determine if the person:
- Is respectful of boundaries
- Exhibits healthy vs. manipulative behavior
- Displays intolerance, controlling tendencies, or red flag patterns

Return a JSON object with:
- A brief behavioral summary
- 2–5 tags like: "safe", "toxic", "controlling", "love-bombing", "intolerant", "cheater", "boundary-violator", etc.

ONLY use the exact format below. No extra explanations.

{{
  "summary": "...",
  "tags": ["...", "..."]
}}


If the person seems safe, no red flags, not a sex offender or on the fbi wanted list, write:
{{
  "summary": "No concerns found.",
  "tags": ["safe"]
}}

Extracted Info:
{combined}
"""
    try:
        response = model.generate_content(prompt)
        raw_text = response.text.strip()
        print("🔎 GEMINI RAW RESPONSE:\n", raw_text)
        match = re.search(r'{\s*"summary"\s*:\s*".+?",\s*"tags"\s*:\s*\[.*?\]}', raw_text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        match = re.search(r'{.*}', raw_text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        print("❌ No valid JSON structure found in Gemini response.")
        return None
    except Exception as e:
        print("❌ Gemini parsing error:", e)
        return None


def extract_text_from_image(image_path):
    try:
        img = Image.open(image_path)
        config = r'--oem 3 --psm 6'
        return pytesseract.image_to_string(img, config=config).strip()
    except Exception as e:
        print("❌ OCR error:", e)
        return ""

def extract_name_guess(text):
    lines = text.splitlines()
    candidates = []

    for line in lines:
        line = line.strip()
        if not line or len(line) > 40:
            continue
        if re.search(r"\b(?:[A-Z][a-z]+ ){1,2}[A-Z][a-z]+\b", line):  # "John Doe" or "John R Doe"
            candidates.append(line.strip())
    
    # Prioritize matches with age/distance patterns (e.g., "Mike, 29")
    for line in candidates:
        if re.match(r"[A-Z][a-z]+,? \d\d", line):
            return line.split(",")[0].strip()

    return candidates[0] if candidates else ""




def name_similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def search_fbi_api(name):
    try:
        response = requests.get("https://api.fbi.gov/wanted/v1/list", params={"title": name})
        results = response.json()
        for item in results.get("items", []):
            if name_similarity(name, item.get("title", "")) > 0.9:
                return {"title": item.get("title"), "description": item.get("description"), "tags": ["🚨 FBI API Match"]}
    except Exception as e:
        print("❌ FBI API error:", e)
    return None

def search_offenders_io(name):
    try:
        headers = {"Authorization": f"Bearer {OFFENDERS_IO_KEY}"}
        key = OFFENDERS_IO_KEY

        # Split name into first and last name
        parts = name.strip().split()
        if len(parts) < 2:
            print("❌ Not enough parts in name to perform offender search.")
            return None

        first_name, last_name = parts[0], " ".join(parts[1:])

        url = f"https://api.offenders.io/sexoffender?key={key}&firstName={first_name}&lastName={last_name}"

        print("📡 Offenders.io URL:", url)
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            data = response.json()
            offenders = data.get("offenders", [])
            if offenders:
                best = offenders[0]
                return {
                    "name": f"{best.get('firstName', '')} {best.get('lastName', '')}",
                    "charges": best.get("sex_offender_charges", "Charges unknown"),
                    "image": best.get("offenderImageUrl"),
                    "link": best.get("offenderUrl"),
                    "raw": best
                }
    except Exception as e:
        print("❌ Offenders.io error:", e)
    return None


def search_offenders_io_face(image_path):
    try:
        key = OFFENDERS_IO_KEY
        url = f"https://faces.api.offenders.io/faces?key={key}"

        headers = {
            "Content-Type": "application/json"
        }

        with open(image_path, "rb") as img_file:
            raw_bytes = img_file.read()
            print("📏 Uploaded image size:", len(raw_bytes), "bytes")
            img_base64 = base64.b64encode(raw_bytes).decode("utf-8")

        # Clean any potential prefix
        if img_base64.startswith("data:image"):
            img_base64 = img_base64.split(",", 1)[1]

        print("📡 Sending request to Offenders.io (query param style)...")
        response = requests.post(url, headers=headers, json={"queryImageBase64": img_base64})

        print("📥 Response code:", response.status_code)
        print("📜 Response body:", response.text)

        if response.status_code == 200:
            return response.json().get("faceMatches", [])
    except Exception as e:
        print("❌ Offenders.io face error:", e)
    return []


def search_scam_data(name):
    links = search_web(f"{name} scam")
    scam_result = get_gemini_summary(name, links)
    if scam_result:
        tags = scam_result.get("tags", [])
        if any(t in tags for t in ["scammer", "fraud", "phishing", "unsafe"]):
            return {
                "summary": scam_result.get("summary"),
                "tags": [t for t in tags if t in ["scammer", "fraud", "phishing", "unsafe"]]
            }
    return None


@app.route("/", methods=["GET", "POST"])
def index():
    summary = ""
    tags = []
    links = []
    extracted = ""
    guessed_name = ""
    final_name = ""
    matched_faces = []
    face_tags = []
    override_gemini = False

    if request.method == "POST":
        form_name = request.form.get("name", "").strip()
        uploaded_file = request.files.get("image")
        pasted_data = request.form.get("pastedImage")

        if pasted_data and pasted_data.startswith("data:image") and len(pasted_data) > 30:
            try:
                header, encoded = pasted_data.split(",", 1)
                image_data = base64.b64decode(encoded)
                if len(image_data) > 8_000_000:
                    return "❌ Pasted image too large.", 400
                image_path = os.path.join("static/uploads", "pasted.png")
                with open(image_path, "wb") as f:
                    f.write(image_data)
                extracted = extract_text_from_image(image_path)
                guessed_name = extract_name_guess(extracted)
                final_name = form_name or guessed_name
            except Exception as e:
                return f"❌ Error reading pasted image: {str(e)}", 400

        elif uploaded_file and uploaded_file.filename != "":
            filename = secure_filename(uploaded_file.filename)
            image_path = os.path.join("static/uploads", filename)
            uploaded_file.save(image_path)

            # Offenders.io facial search with similarity check
            offender_face_result = search_offenders_io_face(image_path)
            if offender_face_result:
                best = offender_face_result[0]
                similarity = float(best.get("similarity", 0))
                offender = best.get("offender", {})

                name_match = f"{offender.get('firstName', '')} {offender.get('lastName', '')}".strip()
                profile_url = offender.get("offenderUrl")

                matched_faces.append({
                    "match": name_match or "Unknown",
                    "confidence": f"{similarity:.2f}%",
                    "source": profile_url or "Offenders.io",
                    "image": offender.get("offenderImageUrl")
                    })

                # Add the source link
                if profile_url:
                    links.append({"title": "Offender Profile", "link": profile_url})

                if similarity >= 80.0:
                    tags.append("🚨 Sex Offender Facial Match")
                    summary += f"\n\n🔴 Match: {name_match} — Offender record found."
                    log_face_match(name_match, best, similarity)
                    final_name = name_match
                    override_gemini = True
                    tags.append("🧠 Name inferred from face match")
                else:
                    summary += f"\n\n⚠️ Partial match ({similarity:.2f}% similarity). Use caution interpreting this result."

            else:
                extracted = extract_text_from_image(image_path)
                guessed_name = extract_name_guess(extracted)
                final_name = final_name or form_name or guessed_name
        else:
            final_name = form_name

        if not final_name and not extracted:
            return render_template("index.html", summary="", tags=[], links=[], extracted="", error="❌ No name or readable text found.", matched_faces=[])

        search_query = f"{final_name} {extracted}".strip()
        print("🧾 OCR Text:", extracted)
        print("🔍 Final Query:", search_query)

        # FBI Match
        fbi_data = search_fbi_api(final_name)
        if fbi_data:
            tags.append("🚨 FBI API Match")
            summary += f"🔴 FBI Data: {fbi_data['title']} — {fbi_data.get('description','')[:200]}..."
            log_fbi_result(search_query, fbi_data)
            override_gemini = True

        # Offenders.io Match
        offender_data = search_offenders_io(final_name)
        if offender_data:
            tags.extend(["🚨 Sex Offender Registry Match", "sex_offender"])

            summary += f"\n\n🔴 Offender Registry Match: {offender_data['charges']}"

            matched_faces.append({
                "match": offender_data["name"],
                "confidence": "Name match (requires visual check)",
                "source": offender_data["link"],
                "image": offender_data["image"],
                "match_type": "name_only"
            })

            log_scan_result("offenders.io", search_query, offender_data["raw"], tags)
            override_gemini = True
        
        print("👮 Offender data match:", offender_data)
        if offender_data:
            override_gemini = True 


        # Scam Check
        scam_data=None
        if override_gemini==False:
            scam_data = search_scam_data(final_name)
            if scam_data:
                tags.append("🚨 Scam Mentioned")
                summary += f"\n\n🔴 Scam Warning: {scam_data['summary']}"
                override_gemini=True

        # Search Web and Analyze
        
        links = search_web(search_query)
        if override_gemini==False:
            gemini_data = get_gemini_summary(search_query or form_name, links)
            if gemini_data:
                ai_summary = gemini_data.get("summary", "")
                if ai_summary.strip():
                    summary += f"\n\n🧠 AI Notes:\n{ai_summary}"
                    tags.extend(gemini_data.get("tags", []))
        else:
            print("⚠️ Skipped Gemini because high-confidence match already found (FBI/Offenders.io)")

        
        danger_keywords = ["scam", "fraud", "offender", "abuse", "toxic", "cheater", "manipulative", "unsafe", "violent", "fbi", "predator", "exploit", "sex crime"]
        summary_lc = summary.lower()

        real_flags = [t for t in tags if any(flag in t.lower() for flag in ["🚨", "⚠️", "scammer", "fraud", "criminal", "sex offender", "abuser", "toxic"])]

        # Also scan summary text for red-flag terms
        if any(k in summary_lc for k in danger_keywords):
            real_flags.append("summary_flag")

        # Only add "safe" if truly no concerns
        if not real_flags and not matched_faces:
            tags.append("safe")


        log_scan_result("final_summary", final_name, {"summary": summary.strip()}, list(set(tags + face_tags)))

    all_tags = list(set(tags + face_tags))
    return render_template("index.html", summary=summary.strip(), tags=all_tags, links=links, extracted=extracted, name=final_name, matched_faces=matched_faces)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, host="0.0.0.0", port=port)