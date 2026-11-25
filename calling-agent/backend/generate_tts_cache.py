"""
generate_tts_cache.py — Pre-generate WAV files for the TTS cache system.

Usage:
    python generate_tts_cache.py                  # generate all missing entries
    python generate_tts_cache.py --dry-run        # print what would be generated
    python generate_tts_cache.py --key greeting   # regenerate one key (all languages)
    python generate_tts_cache.py --validate       # check all WAVs on disk
    python generate_tts_cache.py --force          # overwrite even if file already exists
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import wave
from pathlib import Path

import httpx

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
CACHE_DIR = SCRIPT_DIR.parent / "tts_cache"
MANIFEST_PATH = CACHE_DIR / "manifest.json"

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
TTS_MODEL = "bulbul:v3"
TTS_SPEAKER = "shubh"

LANG_CODE_MAP = {
    "en": "en-IN",
    "hi": "hi-IN",
    "gu": "gu-IN",
}

# ──────────────────────────────────────────────────────────────────────────────
# Full catalogue
# ──────────────────────────────────────────────────────────────────────────────

CACHE_CATALOGUE: dict[str, dict[str, str]] = {

    # ── System phrases ────────────────────────────────────────────────────────

    "greeting": {
        "en": "Hello! Namaste, this is Vishal Patel calling from Pramukh Group. Hope I haven't caught you at a bad time.",
        "hi": "नमस्ते! मैं विशाल पटेल बोल रहा हूँ, प्रमुख ग्रुप से। उम्मीद है मैंने गलत वक्त पर कॉल नहीं किया।",
        "gu": "નમસ્તે! હું વિશાલ પટેલ બોલી રહ્યો છું, પ્રમુખ ગ્રૂપ તરફથી. આશા છે કે મેં ખોટા સમયે ફોન નથી કર્યો.",
    },

    "mid_call_greeting": {
        "en": "Hello, can you hear me?",
        "hi": "हेलो, क्या आपको मेरी आवाज़ आ रही है?",
        "gu": "હેલો, શું તમને મારો અવાજ સંભળાય છે?",
    },

    "shortcut_hello": {
        "en": "Hello! Yes, I'm here. How can I help you?",
        "hi": "हेलो! हाँ, मैं यहाँ हूँ। बताइए, कैसे मदद कर सकता हूँ?",
        "gu": "હેલો! હા, હું અહીં છું. કહો, કઈ રીતે મદદ કરી શકું?",
    },

    "shortcut_bye": {
        "en": "Thank you for your time. Have a great day! We'll follow up shortly.",
        "hi": "आपका समय देने के लिए धन्यवाद। आपका दिन शुभ हो! हम जल्द ही संपर्क करेंगे।",
        "gu": "તમારો સમય આપવા બદલ ધન્યવાદ. તમારો દિવસ સારો રહે! અમે ટૂંક સમયમાં સંપર્ક કરીશું.",
    },

    "shortcut_thanks": {
        "en": "You're most welcome! Is there anything else I can help you with?",
        "hi": "बिल्कुल! क्या कोई और जानकारी चाहिए आपको?",
        "gu": "ખૂબ ખૂબ આભાર! શું બીજી કોઈ જાણકારી જોઈએ છે?",
    },

    "shortcut_not_interested": {
        "en": "No problem at all. If you ever change your mind, we're always here. Have a wonderful day!",
        "hi": "कोई बात नहीं। अगर कभी मन बदलें तो हम हमेशा यहाँ हैं। आपका दिन शुभ हो!",
        "gu": "કોઈ વાત નહીં. ક્યારેક મન બદલાય તો અમે હંમેશા અહીં છીએ. તમારો દિવસ સારો રહે!",
    },

    "shortcut_busy": {
        "en": "Of course, I understand. When would be a better time to call back?",
        "hi": "बिल्कुल समझ सकता हूँ। कब कॉलबैक करें तो ठीक रहेगा?",
        "gu": "બિલકુલ સમજી શકું છું. ક્યારે કૉલ કરીએ તો સારું રહેશે?",
    },

    "stt_fallback": {
        "en": "I'm sorry, I didn't quite catch that. Could you please repeat?",
        "hi": "माफ़ करें, मैं समझ नहीं पाया। क्या आप दोबारा बोल सकते हैं?",
        "gu": "માફ કરશો, મને સમજ ન પડ્યું. શું તમે ફરીથી કહેશો?",
    },

    "language_confirm_hi": {
        "hi": "ज़रूर, मैं हिंदी में बात कर सकता हूँ। बताइए क्या जानना है?",
    },

    "language_confirm_gu": {
        "gu": "ચોક્કસ, હું ગુજરાતીમાં વાત કરી શકું છું. કહો, શું જાણવું છે?",
    },

    # ── Company / brand FAQs ──────────────────────────────────────────────────

    "faq_about_company": {
        "en": "Pramukh Group is a leading real estate developer based in Gujarat with decades of experience. We have successfully delivered numerous projects across Ahmedabad and surrounding areas, with a strong focus on quality construction and timely delivery.",
        "hi": "प्रमुख ग्रुप गुजरात का एक प्रमुख रियल एस्टेट डेवलपर है जिसके पास दशकों का अनुभव है। हमने अहमदाबाद और आसपास के क्षेत्रों में कई परियोजनाएं सफलतापूर्वक पूरी की हैं।",
        "gu": "પ્રમુખ ગ્રૂપ ગુજરાતની અગ્રણી રિયલ એસ્ટેટ ડેવલપર કંપની છે, જેની પાસે દાયકાઓનો અનુભવ છે. અમે અમદાવાદ અને આસપાસના વિસ્તારોમાં અનેક પ્રોજેક્ટ સફળતાપૂર્વક ડિલિવર કર્યા છે.",
    },

    "faq_trust_track_record": {
        "en": "We have a strong track record of on-time delivery and transparent dealings. All our projects are RERA registered, and we have thousands of happy homeowners across Gujarat who trust the Pramukh name.",
        "hi": "हमारा समय पर डिलीवरी और पारदर्शी व्यवहार का मज़बूत ट्रैक रिकॉर्ड है। हमारे सभी प्रोजेक्ट RERA रजिस्टर्ड हैं और गुजरात भर में हजारों संतुष्ट गृहस्वामी हैं।",
        "gu": "અમારી સમયસર ડિલિવરી અને પારદર્શક વ્યવહારની મજબૂત ટ્રેક રેકોર્ડ છે. અમારા બધા પ્રોજેક્ટ RERA રજિસ્ટર્ડ છે અને ગુજરાતભરમાં હજારો સુખી ઘર-માલિકો પ્રમુખ નામ પર વિશ્વાસ કરે છે.",
    },

    "faq_rera": {
        "en": "Yes, all our projects are fully RERA registered. You can verify the registration number on the RERA Gujarat website anytime for complete transparency and peace of mind.",
        "hi": "हाँ, हमारे सभी प्रोजेक्ट पूरी तरह RERA रजिस्टर्ड हैं। पारदर्शिता और मानसिक शांति के लिए आप RERA गुजरात वेबसाइट पर कभी भी रजिस्ट्रेशन नंबर वेरिफ़ाई कर सकते हैं।",
        "gu": "હા, અમારા બધા પ્રોજેક્ટ સંપૂર્ણ RERA રજિસ્ટર્ડ છે. પારદર્શિતા અને મનની શાંતિ માટે તમે RERA ગુજરાત વેબસાઇટ પર ક્યારે પણ રજિસ્ટ્રેશન નંબર ચકાસી શકો છો.",
    },

    # ── Project FAQs ─────────────────────────────────────────────────────────

    "faq_projects_overview": {
        "en": "We currently have several exciting projects across Ahmedabad and surrounding areas — residential apartments, row houses, and plots. Could you tell me which area you're looking at? I can then give you the most relevant details.",
        "hi": "हमारे पास अभी अहमदाबाद और आसपास के इलाकों में कई बेहतरीन प्रोजेक्ट हैं — रेज़िडेंशियल अपार्टमेंट्स, रो हाउस और प्लॉट्स। आप किस इलाके में देख रहे हैं? मैं तब सबसे उचित जानकारी दे सकता हूँ।",
        "gu": "અમારી પાસે હાલ અમદાવાદ અને આસપાસ અનેક સુંદર પ્રોજેક્ટ છે — રેઝિડેન્શિયલ અપાર્ટમેન્ટ, રો હાઉસ અને પ્લૉટ. આપ ક્યા વિસ્તારમાં જોઈ રહ્યા છો? હું પછી સૌથી સંબંધિત માહિતી આપી શકીશ.",
    },

    # ── Pricing / financial FAQs ──────────────────────────────────────────────

    "faq_price_range_general": {
        "en": "Our projects are priced to suit a range of budgets, from affordable starter homes to premium residences. Could you share your approximate budget? I can match you with the right project right away.",
        "hi": "हमारे प्रोजेक्ट विभिन्न बजट के अनुसार हैं, किफायती पहले घर से लेकर प्रीमियम आवासों तक। आपका अनुमानित बजट क्या है? मैं तुरंत आपके लिए सही प्रोजेक्ट बता सकता हूँ।",
        "gu": "અમારા પ્રોજેક્ટ વિવિધ બજેટ માટે છે, સસ્તા સ્ટાર્ટર હોમ્સથી પ્રીમિયમ રેઝિડેન્સ સુધી. શું આપ આપનો અંદાજિત બજેટ જણાવી શકો? હું તરત જ આપ માટે યોગ્ય પ્રોજેક્ટ બતાવી શકીશ.",
    },

    "faq_payment_plan": {
        "en": "We offer flexible payment plans including construction-linked plans, down payment plans, and easy EMI options. We also have tie-ups with leading banks for home loans at competitive rates.",
        "hi": "हम कंस्ट्रक्शन-लिंक्ड प्लान, डाउन पेमेंट प्लान और आसान EMI विकल्प प्रदान करते हैं। प्रतिस्पर्धी दरों पर होम लोन के लिए प्रमुख बैंकों के साथ हमारे टाई-अप भी हैं।",
        "gu": "અમે કન્સ્ટ્રક્શન-લિંક્ડ પ્લાન, ડાઉન પેમેન્ટ પ્લાન અને સરળ EMI વિકલ્પ ઓફર કરીએ છીએ. સ્પર્ધાત્મક દરે હોમ લોન માટે અગ્રણી બેન્ક સાથે ટાઇ-અપ પણ છે.",
    },

    "faq_home_loan": {
        "en": "Yes, we have tie-ups with SBI, HDFC, ICICI, and other leading banks. Our relationship managers can help you get pre-approved at the best interest rates. Would you like me to arrange a callback from our loan desk?",
        "hi": "हाँ, हमारे SBI, HDFC, ICICI और अन्य प्रमुख बैंकों के साथ टाई-अप हैं। हमारे रिलेशनशिप मैनेजर्स आपको सर्वोत्तम ब्याज दर पर पूर्व-अनुमोदन दिलाने में मदद करेंगे। क्या मैं हमारे लोन डेस्क से कॉलबैक अरेंज करूँ?",
        "gu": "હા, અમારા SBI, HDFC, ICICI અને અન્ય અગ્રણી બેન્ક સાથે ટાઇ-અપ છે. અમારા રિલેશનશિપ મેનેજર આપને શ્રેષ્ઠ વ્યાજ દરે પ્રી-એપ્રૂવ્ડ કરાવવામાં મદદ કરશે. શું હું અમારા લોન ડેસ્ક તરફથી કૉલ-બૅક ગોઠવી દઉં?",
    },

    "faq_booking_amount": {
        "en": "The booking amount is a small token that reserves the unit in your name and is fully adjustable against the final payment. Our team can walk you through the exact figures when you visit.",
        "hi": "बुकिंग राशि एक छोटी टोकन रकम है जो यूनिट आपके नाम पर आरक्षित करती है और अंतिम भुगतान में पूरी तरह समायोजित हो जाती है। हमारी टीम विज़िट के दौरान आपको सटीक आंकड़े बता सकती है।",
        "gu": "બુકિંગ રકમ એ એક નાની ટોકન રકમ છે જે એકમ આપના નામ પર અનામત કરે છે અને અંતિમ ચૂકવણી સામે સંપૂર્ણ ઍડ્જસ્ટ થઈ જાય છે. અમારી ટીમ વિઝિટ દરમ્યાન આપને ચોક્કસ આંકડા જણાવી શકે છે.",
    },

    # ── Location / connectivity FAQs ──────────────────────────────────────────

    "faq_location_connectivity": {
        "en": "Our projects are strategically located with excellent connectivity to key landmarks, schools, hospitals, and transport hubs. The areas have seen strong appreciation over the past few years, making them great for both living and investment.",
        "hi": "हमारे प्रोजेक्ट प्रमुख लैंडमार्क, स्कूल, अस्पताल और ट्रांसपोर्ट हब से बेहतरीन कनेक्टिविटी के साथ रणनीतिक रूप से स्थित हैं। पिछले कुछ वर्षों में इन इलाकों में मजबूत सराहना देखी गई है।",
        "gu": "અમારા પ્રોજેક્ટ મુખ્ય સ્થળો, શાળઓ, હૉસ્પિટલ અને ટ્રાન્સપૉર્ટ હબ સાથે ઉત્તમ કનેક્ટિવિટી સાથે વ્યૂહાત્મક રીતે સ્થિત છે. છેલ્લા કેટલાક વર્ષોમાં આ વિસ્તારોમાં મજબૂત વૃદ્ધિ જોવા મળી છે.",
    },

    "faq_nearby_amenities": {
        "en": "The project is surrounded by top schools, hospitals, shopping malls, and parks — all within easy reach. Daily life is extremely convenient with everything you need just minutes away.",
        "hi": "प्रोजेक्ट के आसपास टॉप स्कूल, अस्पताल, शॉपिंग मॉल और पार्क हैं — सभी आसानी से पहुँच में। रोज़मर्रा की ज़िंदगी बेहद सुविधाजनक है क्योंकि ज़रूरत की हर चीज़ कुछ ही मिनटों की दूरी पर है।",
        "gu": "પ્રોજેક્ટની આસપાસ ટૉપ સ્કૂલ, હૉસ્પિટલ, શૉપિંગ મૉલ અને પાર્ક — બધું સહેલાઈથી પહોંચી શકાય તેટલી નજીક છે. દૈનિક જીવન ખૂબ સગવડભર્યું છે કારણ કે જરૂરી દરેક ચીજ માત્ર થોડી મિનિટ દૂર છે.",
    },

    # ── Construction / quality FAQs ───────────────────────────────────────────

    "faq_construction_quality": {
        "en": "We use premium materials throughout — RCC frame structure, branded fittings, vitrified flooring, and branded sanitary ware. All buildings are earthquake-resistant and vastu-compliant by design.",
        "hi": "हम पूरी तरह प्रीमियम सामग्री का उपयोग करते हैं — RCC फ्रेम स्ट्रक्चर, ब्रांडेड फिटिंग्स, विट्रिफाइड फ्लोरिंग और ब्रांडेड सैनिटरी वेयर। सभी बिल्डिंग डिज़ाइन से भूकंप-प्रतिरोधी और वास्तु-अनुपालन हैं।",
        "gu": "અમે સમગ્ર પ્રીમિયમ સામગ્રીનો ઉપયોગ કરીએ છીએ — RCC ફ્રેમ સ્ટ્રક્ચર, બ્રાન્ડેડ ફિટિંગ, વિટ્રિફાઇડ ફ્લોરિંગ અને બ્રાન્ડેડ સૅનિટરી વેર. બધી ઇમારતો ડિઝાઇન દ્વારા ભૂકંપ-પ્રતિરોધી અને વાસ્તુ-અનુપાલિત છે.",
    },

    "faq_possession_timeline": {
        "en": "We are known for on-time or early delivery. Our team keeps buyers regularly updated through progress reports and site visit updates so you always know the current status.",
        "hi": "हम समय पर या जल्दी डिलीवरी के लिए जाने जाते हैं। हमारी टीम प्रगति रिपोर्ट और साइट विज़िट अपडेट के माध्यम से खरीदारों को नियमित रूप से अपडेट करती है।",
        "gu": "અમે સમયસર અથવા વહેલા ડિલિવરી માટે જાણીતા છીએ. અમારી ટીમ પ્રગ્રેસ રિપૉર્ટ અને સાઇટ વિઝિટ અપડેટ દ્વારા ખરીદદારોને નિયમિત અપડેટ રાખે છે.",
    },

    "faq_amenities_general": {
        "en": "Our projects typically include a clubhouse, swimming pool, gymnasium, children's play area, landscaped gardens, twenty-four seven security, and covered parking. Premium projects also include a rooftop lounge and co-working spaces.",
        "hi": "हमारे प्रोजेक्ट में आमतौर पर क्लबहाउस, स्विमिंग पूल, जिम, बच्चों का खेल क्षेत्र, लैंडस्केप्ड गार्डन, चौबीसों घंटे सुरक्षा और कवर्ड पार्किंग शामिल हैं। प्रीमियम प्रोजेक्ट में रूफटॉप लाउंज और को-वर्किंग स्पेस भी हैं।",
        "gu": "અમારા પ્રોજેક્ટ્સમાં ક્લબહાઉસ, સ્વિમિંગ પૂલ, જિમ, બાળ-ક્રીડા ક્ષેત્ર, લેન્ડસ્કેપ્ડ ગાર્ડન, ચોવીસ કલાક સુરક્ષા અને કવર્ડ પાર્કિંગ સામાન્ય રીતે સામેલ છે. પ્રીમિયમ પ્રોજેક્ટ્સમાં રૂફટૉપ લૉઉન્જ અને કો-વર્કિંગ સ્પેસ પણ છે.",
    },

    # ── Visit / appointment phrases ───────────────────────────────────────────

    "faq_site_visit": {
        "en": "Absolutely, we'd love to have you visit! Our site is open daily and we arrange guided tours with our sales team who can walk you through the model flat and answer all your questions in person.",
        "hi": "बिल्कुल, हम आपका स्वागत करेंगे! हमारी साइट रोज़ खुली रहती है और हम अपनी सेल्स टीम के साथ गाइडेड टूर आयोजित करते हैं जो आपको मॉडल फ्लैट दिखाएगी और आपके सभी सवालों के जवाब देगी।",
        "gu": "ચોક્કસ, અમે આપનું સ્વાગત કરીશું! અમારી સાઇટ રોજ ખુલ્લી રહે છે અને અમારી સેલ્સ ટીમ સાથે ગાઇડેડ ટૂર ગોઠવીએ છીએ, જે તમને મૉડલ ફ્લૅટ બતાવશે અને તમારા બધા પ્રશ્નોના જવાબ આપશે.",
    },

    "invite_visit": {
        "en": "Based on what you've told me, I think you'd love seeing it in person. Can I arrange a site visit for you this week? It won't take more than an hour and our team will take complete care of you.",
        "hi": "आपने जो बताया उसके आधार पर, मुझे लगता है आप इसे खुद देखकर पसंद करेंगे। क्या मैं इस हफ्ते आपके लिए साइट विज़िट अरेंज कर सकता हूँ? इसमें एक घंटे से ज़्यादा नहीं लगेगा और हमारी टीम पूरा ख्याल रखेगी।",
        "gu": "આપે જે જણાવ્યું તેના આધારે, મને લાગે છે આપ તેને સ્વ-આંખે જોઈ ગમશે. શું હું આ અઠવાડિયે આપ માટે સાઇટ વિઝિટ ગોઠવી શકું? એક કલાકથી વધારે સમય નહીં લાગે અને અમારી ટીમ સંપૂર્ણ સંભાળ લેશે.",
    },

    "ask_appointment_date": {
        "en": "Great! What date and time works best for you? We have slots available on weekdays and weekends both.",
        "hi": "बढ़िया! कौन सी तारीख और समय आपके लिए सबसे अच्छा रहेगा? हमारे पास वीकडे और वीकेंड दोनों पर स्लॉट उपलब्ध हैं।",
        "gu": "ઉત્તમ! કઈ તારીખ અને સમય આપ માટે સૌથી સારો રહેશે? અઠવાડિયાના દિવસ અને સપ્તાહાંત બંનેમાં સ્લૉટ ઉપલબ્ધ છે.",
    },

    "appointment_confirmed": {
        "en": "Perfect, your visit is confirmed! Our team will call you a day before to confirm the directions and any special requirements. We look forward to meeting you.",
        "hi": "बढ़िया, आपकी विज़िट कन्फ़र्म हो गई! हमारी टीम डायरेक्शन और किसी विशेष आवश्यकता की पुष्टि के लिए एक दिन पहले कॉल करेगी। आपसे मिलने का इंतज़ार रहेगा।",
        "gu": "ઉત્તમ, આપની વિઝિટ કન્ફર્મ થઈ ગઈ! અમારી ટીમ દિશા-નિર્દેશ અને કોઈ ખાસ જરૂરિયાત માટે એક દિવસ પહેલા ફોન કરશે. આપને મળવાની ઉત્સુકતા છે.",
    },

    # ── Stage transition phrases ──────────────────────────────────────────────

    "ask_name": {
        "en": "May I know your good name, please?",
        "hi": "क्या मैं आपका नाम जान सकता हूँ?",
        "gu": "શું હું આપનું નામ જાણી શકું?",
    },

    "ask_property_type": {
        "en": "What type of property are you looking for — an apartment, row house, or a plot? And are you looking for it as a home to live in, or as an investment?",
        "hi": "आप किस तरह की प्रॉपर्टी ढूंढ रहे हैं — अपार्टमेंट, रो हाउस या प्लॉट? और क्या यह रहने के लिए है या निवेश के लिए?",
        "gu": "આપ કયા પ્રકારની પ્રૉપર્ટી શોધી રહ્યા છો — અપાર્ટમેન્ટ, રો હાઉસ અથવા પ્લૉટ? અને શું તે રહેવા માટે છે કે રોકાણ માટે?",
    },

    "ask_budget": {
        "en": "That's helpful, thank you. Could you give me a rough idea of your budget? This will help me suggest the most suitable options for you.",
        "hi": "यह जानकर अच्छा लगा, शुक्रिया। क्या आप अपने बजट का एक अनुमान दे सकते हैं? इससे मुझे आपके लिए सबसे उपयुक्त विकल्प सुझाने में मदद मिलेगी।",
        "gu": "આ સાંભળી ખૂબ ગમ્યું, ધન્યવાદ. શું આપ આપના બજેટનો અંદાજ આપી શકો? આ મને આપ માટે સૌથી યોગ્ય વિકલ્પ સૂચવવામાં મદદ કરશે.",
    },

    "ask_location_preference": {
        "en": "Do you have any preference for the area or part of the city? For example, are you looking at the western suburbs, the eastern corridor, or somewhere closer to the city centre?",
        "hi": "क्या आपकी कोई क्षेत्र या शहर के किसी हिस्से की प्राथमिकता है? उदाहरण के लिए, क्या आप वेस्टर्न सबर्ब, ईस्टर्न कॉरिडोर, या शहर के केंद्र के नज़दीक देख रहे हैं?",
        "gu": "શું આપ કોઈ ચોક્કસ વિસ્તાર અથવા શહેરના ભાગ માટે પ્રાથમિકતા ધરાવો છો? ઉદાહરણ તરીકે, શું આપ પશ્ચિમ ઉપનગર, પૂર્વ કૉરિડૉર, અથવા શહેર-કેન્દ્રની નજીક જોઈ રહ્યા છો?",
    },

    "confirm_interest": {
        "en": "That sounds like a great fit! Based on everything you've shared, this project seems ideal for your needs. Would you like to know more about it?",
        "hi": "यह बिल्कुल सही लग रहा है! आपने जो बताया उसके आधार पर, यह प्रोजेक्ट आपकी ज़रूरतों के लिए आदर्श लगता है। क्या आप इसके बारे में और जानना चाहेंगे?",
        "gu": "આ ખૂબ સારું મળ્યું! આપે જે જણાવ્યું તેના આધારે, આ પ્રોજેક્ટ આપની જરૂરિયાત માટે આદર્શ લાગે છે. શું આ વિશે વધુ જાણવું છે?",
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    return {}


def save_manifest(manifest: dict) -> None:
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


def wav_info(path: Path) -> tuple[float, int]:
    """Return (duration_s, file_size_bytes) for a WAV file."""
    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        duration = frames / float(rate)
    return duration, path.stat().st_size


def call_sarvam_tts(text: str, lang: str, api_key: str) -> bytes:
    """Call Sarvam TTS API and return raw WAV bytes."""
    lang_code = LANG_CODE_MAP.get(lang, f"{lang}-IN")
    payload = {
        "inputs": [text],
        "target_language_code": lang_code,
        "speaker": TTS_SPEAKER,
        "model": TTS_MODEL,
        "enable_preprocessing": True,
    }
    headers = {
        "Content-Type": "application/json",
        "API-Subscription-Key": api_key,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(SARVAM_TTS_URL, json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    audios = data.get("audios", [])
    if not audios:
        raise ValueError(f"No audio in Sarvam response: {data}")
    return base64.b64decode(audios[0])


def validate_wav_bytes(wav_bytes: bytes, key: str, lang: str) -> float:
    """Parse WAV bytes, assert duration > 0.3s, return duration."""
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        duration = frames / float(rate)
    if duration < 0.3:
        raise ValueError(
            f"[{key}/{lang}] WAV too short: {duration:.2f}s (expected > 0.3s)"
        )
    return duration


# ──────────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────────

def cmd_dry_run(filter_key: str | None) -> None:
    total = 0
    for key, langs in CACHE_CATALOGUE.items():
        if filter_key and key != filter_key:
            continue
        for lang, text in langs.items():
            fname = f"{key}_{lang}.wav"
            preview = text[:60].replace("\n", " ")
            print(f"  [{lang}] {fname:<45} {len(text):>4} chars  \"{preview}...\"")
            total += 1
    print(f"\nDry run: {total} file(s) would be generated.")


def cmd_generate(api_key: str, filter_key: str | None, force: bool) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()

    total_generated = 0
    total_skipped = 0
    total_failed = 0
    first_call = True

    for key, langs in CACHE_CATALOGUE.items():
        if filter_key and key != filter_key:
            continue
        for lang, text in langs.items():
            manifest_key = f"{key}_{lang}"
            out_path = CACHE_DIR / f"{manifest_key}.wav"

            if out_path.exists() and not force:
                print(f"  SKIP  {manifest_key}.wav  (already exists, use --force to overwrite)")
                total_skipped += 1
                continue

            if not first_call:
                time.sleep(1.0)
            first_call = False

            try:
                print(f"  GEN   {manifest_key}.wav  ({len(text)} chars) ...", end="", flush=True)
                wav_bytes = call_sarvam_tts(text, lang, api_key)
                duration = validate_wav_bytes(wav_bytes, key, lang)

                out_path.write_bytes(wav_bytes)

                manifest[manifest_key] = {
                    "key": key,
                    "lang": lang,
                    "path": str(out_path),
                    "duration_s": round(duration, 3),
                    "chars": len(text),
                }
                print(f" OK  ({duration:.2f}s)")
                total_generated += 1

            except Exception as exc:
                print(f" FAIL  {exc}")
                total_failed += 1

    save_manifest(manifest)
    print(
        f"\nDone. generated={total_generated}  skipped={total_skipped}  failed={total_failed}"
    )
    print(f"Manifest: {MANIFEST_PATH}")


def cmd_validate() -> None:
    if not CACHE_DIR.exists():
        print(f"Cache directory not found: {CACHE_DIR}")
        sys.exit(1)

    manifest = load_manifest()
    all_pass = True

    print(f"\n{'KEY':<35} {'LANG':<5} {'DURATION':>10} {'SIZE':>10}  STATUS")
    print("-" * 70)

    for key, langs in CACHE_CATALOGUE.items():
        for lang in langs:
            manifest_key = f"{key}_{lang}"
            wav_path = CACHE_DIR / f"{manifest_key}.wav"

            if not wav_path.exists():
                print(f"  {key:<33} {lang:<5} {'—':>10} {'—':>10}  FAIL (missing)")
                all_pass = False
                continue

            try:
                duration, size = wav_info(wav_path)
                status = "OK" if duration >= 0.3 else "FAIL (too short)"
                if duration < 0.3:
                    all_pass = False
                print(f"  {key:<33} {lang:<5} {duration:>9.2f}s {size:>9}B  {status}")
            except Exception as exc:
                print(f"  {key:<33} {lang:<5} {'—':>10} {'—':>10}  FAIL ({exc})")
                all_pass = False

    print("-" * 70)
    if all_pass:
        print("All WAVs OK.")
    else:
        print("Some WAVs FAILED — re-run generate_tts_cache.py --key <name> to fix.")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-generate TTS cache WAV files.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be generated without making API calls")
    parser.add_argument("--key", metavar="NAME", help="Generate only this cache key (all languages)")
    parser.add_argument("--validate", action="store_true", help="Check all WAVs on disk without API calls")
    parser.add_argument("--force", action="store_true", help="Overwrite existing WAV files")
    args = parser.parse_args()

    if args.validate:
        cmd_validate()
        return

    if args.dry_run:
        cmd_dry_run(args.key)
        return

    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        print("ERROR: SARVAM_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    cmd_generate(api_key, filter_key=args.key, force=args.force)


if __name__ == "__main__":
    main()
