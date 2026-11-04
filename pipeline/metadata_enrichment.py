import os
import requests
import time
import logging

logger = logging.getLogger(__name__)

class EnrichmentProvider:
    def enrich(self, title: str, author: str) -> dict:
        raise NotImplementedError

class GoogleBooksProvider(EnrichmentProvider):
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.base_url = "https://www.googleapis.com/books/v1/volumes"

    def enrich(self, title: str, author: str) -> dict:
        query_parts = []
        if title:
            query_parts.append(title)
        if author:
            query_parts.append(author)
            
        if not query_parts:
            return None
            
        params = {"q": " ".join(query_parts)}
        if self.api_key:
            params["key"] = self.api_key

        try:
            resp = requests.get(self.base_url, params=params, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("items"):
                    return data["items"][:5]
            elif resp.status_code == 429:
                logger.warning("Google Books API rate limit exceeded.")
        except Exception as e:
            logger.error(f"Google Books API error: {e}")
            
        return None

# Crossref and OpenAIRE ask API clients to identify themselves with a contact
# address (the "polite pool" — it gets you better rate limits and lets them
# reach you if a query misbehaves). Set CROSSREF_EMAIL to your own address;
# the placeholder works but gets no polite-pool treatment.
DEFAULT_CROSSREF_EMAIL = os.environ.get("CROSSREF_EMAIL", "your-email@example.org")


class CrossrefProvider(EnrichmentProvider):
    def __init__(self, email=None):
        email = email or DEFAULT_CROSSREF_EMAIL
        self.email = email
        self.base_url = "https://api.crossref.org/works"

    def enrich(self, title: str, author: str) -> dict:
        params = {}
        if title:
            params["query.bibliographic"] = title
        if author:
            params["query.author"] = author
            
        if not params:
            return None
            
        if self.email:
            params["mailto"] = self.email

        try:
            resp = requests.get(self.base_url, params=params, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("message", {}).get("items", [])
                if items:
                    return items[:5]
        except Exception as e:
            logger.error(f"Crossref API error: {e}")
            
        return None

class OpenAIREProvider(EnrichmentProvider):
    def __init__(self):
        self.base_url = "https://api.openaire.eu/search/publications"

    def enrich(self, title: str, author: str) -> dict:
        params = {"format": "json"}
        if title:
            params["title"] = title
        if author:
            params["author"] = author
            
        if len(params) == 1:
            return None

        try:
            resp = requests.get(self.base_url, params=params, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("response", {}).get("results", {}).get("result", [])
                if results:
                    if not isinstance(results, list):
                        results = [results]
                    
                    normalized_results = []
                    for item in results[:5]:
                        try:
                            oaf = item["metadata"]["oaf:entity"]["oaf:result"]
                            
                            def extract_dollar(val):
                                if isinstance(val, list):
                                    return [extract_dollar(v) for v in val]
                                if isinstance(val, dict):
                                    return val.get("$", "")
                                return str(val)
                                
                            norm = {}
                            title_val = extract_dollar(oaf.get("title", ""))
                            norm["title"] = title_val[0] if isinstance(title_val, list) and title_val else (title_val if isinstance(title_val, str) else "")
                            
                            creator_val = extract_dollar(oaf.get("creator", []))
                            norm["creator"] = creator_val if isinstance(creator_val, list) else [creator_val] if creator_val else []
                            
                            pub_val = extract_dollar(oaf.get("publisher", ""))
                            norm["publisher"] = pub_val[0] if isinstance(pub_val, list) and pub_val else (pub_val if isinstance(pub_val, str) else "")
                            
                            date_val = extract_dollar(oaf.get("dateofacceptance", ""))
                            norm["dateofacceptance"] = date_val[0] if isinstance(date_val, list) and date_val else (date_val if isinstance(date_val, str) else "")
                            
                            normalized_results.append(norm)
                        except Exception as e:
                            logger.error(f"Error parsing OpenAIRE structure: {e}")
                    
                    return normalized_results
        except Exception as e:
            logger.error(f"OpenAIRE API error: {e}")
            
        return None

class EnrichmentService:
    def __init__(self, google_books_api_key=None, crossref_email=None):
        crossref_email = crossref_email or DEFAULT_CROSSREF_EMAIL
        self.providers = {
            "google_books": GoogleBooksProvider(google_books_api_key),
            "crossref": CrossrefProvider(crossref_email),
            "openaire": OpenAIREProvider()
        }
        
    def enrich_reference(self, components: list) -> dict:
        title_comp = next((c for c in components if c["label"] == "Title"), None)
        author_comp = next((c for c in components if c["label"] == "Author"), None)
        year_comp = next((c for c in components if c["label"] == "PublicationYear"), None)
        
        title = title_comp["text"] if title_comp else None
        author = author_comp["text"] if author_comp else None
        
        original_year = None
        if year_comp and year_comp["text"]:
            import re
            m = re.search(r'\b(18|19|20)\d{2}\b', year_comp["text"])
            if m:
                original_year = int(m.group(0))
        
        original_last_name = None
        if author:
            parts = author.split(',')
            if len(parts) > 1:
                original_last_name = parts[0].strip().lower()
            else:
                original_last_name = author.split()[-1].strip().lower()
        
        if not title and not author:
            return {}
            
        results = {}
        for name, provider in self.providers.items():
            res_list = provider.enrich(title, author)
            if res_list:
                valid_candidates = []
                for res in res_list:
                    # Sanity Check: Year
                    res_year = None
                    try:
                        if name == "crossref":
                            issued = res.get("issued", {}).get("date-parts", [[None]])
                            if issued and issued[0] and issued[0][0]:
                                res_year = int(issued[0][0])
                        elif name == "google_books":
                            pub_date = res.get("volumeInfo", {}).get("publishedDate", "")
                            if pub_date and pub_date[:4].isdigit():
                                res_year = int(pub_date[:4])
                        elif name == "openaire":
                            date_accept = res.get("dateofacceptance", "")
                            if date_accept and str(date_accept)[:4].isdigit():
                                res_year = int(str(date_accept)[:4])
                    except Exception as e:
                        logger.debug(f"Year parsing failed for {name}: {e}")

                    if original_year and res_year:
                        if abs(original_year - res_year) > 2:
                            continue # Discard this mismatch

                    # Sanity Check: Author
                    if original_last_name:
                        author_match = False
                        res_authors_text = ""
                        if name == "crossref":
                            authors = res.get("author", [])
                            res_authors_text = " ".join([a.get("family", "") + " " + a.get("given", "") for a in authors]).lower()
                        elif name == "google_books":
                            authors = res.get("volumeInfo", {}).get("authors", [])
                            res_authors_text = " ".join(authors).lower()
                        elif name == "openaire":
                            authors = res.get("creator", [])
                            res_authors_text = " ".join(authors).lower()
                            
                        # Only require author match if the result actually provided authors
                        if res_authors_text and original_last_name not in res_authors_text:
                            # if original_last_name has non-alphabetic chars (e.g. dots, hyphens), try a cleaner version
                            import re
                            clean_last_name = re.sub(r'[^a-z0-9]', '', original_last_name)
                            clean_res_authors = re.sub(r'[^a-z0-9]', '', res_authors_text)
                            if clean_last_name and clean_last_name not in clean_res_authors:
                                continue # Discard this mismatch
                                
                    valid_candidates.append(res)
                
                if valid_candidates:
                    results[name] = valid_candidates
                
            time.sleep(0.5) # Basic rate limiting delay
            
        return results
