import base64
import re
from openai import AsyncOpenAI
from config import OPENAI_API_KEY

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def generate_article(
    topic: str, client_domain: str, anchor_text: str, language: str = "pl",
    anchor_text2: str = "", anchor_url2: str = "",
    anchor_text3: str = "", anchor_url3: str = "",
    custom_prompt: str = "",
    variation_hint: str = "",
) -> dict:
    def clean_url(url):
        url = url.strip()
        if not url.startswith('http://') and not url.startswith('https://'):
            url = 'https://' + url
        return url

    # Build anchor instructions
    anchors_info = f'<a href="{clean_url(client_domain)}">{anchor_text}</a>'
    if anchor_text2 and anchor_url2:
        anchors_info += f', <a href="{clean_url(anchor_url2)}">{anchor_text2}</a>'
    if anchor_text3 and anchor_url3:
        anchors_info += f', <a href="{clean_url(anchor_url3)}">{anchor_text3}</a>'

    variation = f" Kąt tematyczny: {variation_hint}." if variation_hint else ""

    if language == "pl":
        system_prompt = (
            "Jesteś ekspertem SEO. Piszesz unikalne artykuły zoptymalizowane pod wyszukiwarki. "
            "Każdy artykuł musi być unikalny — inny tytuł, inna struktura, inne przykłady, inne podejście do tematu. "
            "Zwracaj treść w formacie HTML (tylko body, bez <html>/<body> tagów). "
            "Artykuł powinien mieć 800-1200 słów, nagłówki H2/H3, akapity. "
            "Dodaj naturalnie linki kotwiczne do strony klienta w treści artykułu."
        )
        if custom_prompt:
            system_prompt += f" Dodatkowe instrukcje: {custom_prompt}"
        user_prompt = (
            f"Napisz unikalny artykuł SEO na temat: '{topic}'.{variation} "
            f"WAŻNE: Wymyśl WŁASNY, ORYGINALNY tytuł artykułu — NIE używaj tematu '{topic}' jako tytułu. "
            f"Tytuł ma być zbliżony tematycznie ale sformułowany inaczej, np. jako pytanie, poradnik lub ciekawostka. "
            f"Użyj innej struktury i podejścia niż standardowe artykuły o tym temacie. "
            f"Umieść w treści KAŻDY z poniższych linków DOKŁADNIE RAZ (nie więcej, nie mniej): {anchors_info}. "
            f"Każdy link może pojawić się w artykule tylko jeden raz. "
            f"Zwróć JSON z polami 'title' (wymyślony tytuł) i 'content' (HTML artykułu). "
            f"Tylko JSON, bez markdown."
        )
    else:
        system_prompt = (
            "You are an SEO expert. You write unique, search engine optimized articles. "
            "Each article must be unique — different title, different structure, different examples, different angle. "
            "Return content in HTML format (body only, no <html>/<body> tags). "
            "Article should be 800-1200 words, with H2/H3 headings and paragraphs. "
            "Include anchor links to the client site naturally in the article. Do not skip any provided link."
        )
        if custom_prompt:
            system_prompt += f" Additional instructions: {custom_prompt}"
        user_prompt = (
            f"Write a unique SEO article about: '{topic}'.{variation} "
            f"IMPORTANT: Create your OWN original title — do NOT use '{topic}' as the title. "
            f"The title should be related but phrased differently, e.g. as a question, guide or insight. "
            f"Use a different structure and angle than typical articles on this topic. "
            f"Include EACH of the following links EXACTLY ONCE in the text (no more, no less): {anchors_info}. "
            f"Each link must appear only one time in the article. "
            f"Return JSON with 'title' (your invented title) and 'content' (HTML article). "
            f"JSON only, no markdown."
        )

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=3000,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    import json
    data = json.loads(raw)
    content = data.get("content", "")

    # Deduplicate links — keep only first occurrence of each href domain
    import re
    seen_hrefs = set()
    def remove_duplicate_link(m):
        href = re.search(r'href=["\']([^"\']+)["\']', m.group(0))
        if not href:
            return m.group(0)
        url = href.group(1).rstrip('/')
        if url in seen_hrefs:
            text = re.sub(r'<[^>]+>', '', m.group(0))
            return text
        seen_hrefs.add(url)
        return m.group(0)

    content = re.sub(r'<a\s[^>]*?>.*?</a>', remove_duplicate_link, content, flags=re.DOTALL | re.IGNORECASE)

    return {
        "title": data.get("title", topic),
        "content": content,
    }


async def describe_image_and_generate(image_b64: str, topic: str) -> str:
    """Describe a static screenshot via vision, then generate a DALL-E image based on it."""
    vision_response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Opisz krótko co widać na tym screenshocie strony (max 2 zdania). Kontekst: artykuł o '{topic}'."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }],
        max_tokens=200,
    )
    description = vision_response.choices[0].message.content
    prompt = f"Professional website screenshot illustration: {description}. Clean, modern design."
    return await generate_image(prompt)


async def generate_image(prompt: str) -> str:
    """Generate image with DALL-E 3 and return base64 string."""
    response = await client.images.generate(
        model="dall-e-3",
        prompt=prompt,
        n=1,
        size="1024x1024",
        response_format="b64_json",
    )
    b64 = response.data[0].b64_json
    return b64
