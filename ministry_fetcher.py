from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup


@dataclass
class MinistryRecallPage:
    url: str
    title: str
    page_text: str
    pdf_url: Optional[str]


class MinistryFetcher:

    def __init__(self):
        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "RichiamiItaliaUpdater/1.0 "
                    "(public food recall data updater)"
                ),
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
            }
        )

    def fetch_recall_page(
        self,
        url: str,
    ) -> MinistryRecallPage:

        response = self.session.get(
            url,
            timeout=30,
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "lxml",
        )

        title = self._extract_title(soup)

        page_text = soup.get_text(
            separator="\n",
            strip=True,
        )

        pdf_url = self._find_pdf_url(
            soup=soup,
            base_url=url,
        )

        return MinistryRecallPage(
            url=url,
            title=title,
            page_text=page_text,
            pdf_url=pdf_url,
        )

    def _extract_title(
        self,
        soup: BeautifulSoup,
    ) -> str:

        h1 = soup.find("h1")

        if h1:
            title = h1.get_text(
                " ",
                strip=True,
            )

            if title:
                return title

        if soup.title:
            title = soup.title.get_text(
                " ",
                strip=True,
            )

            if title:
                return title

        return "Titolo non disponibile"

    def _find_pdf_url(
        self,
        soup: BeautifulSoup,
        base_url: str,
    ) -> Optional[str]:

        from urllib.parse import urljoin

        for link in soup.find_all(
            "a",
            href=True,
        ):

            href = link.get("href", "").strip()

            if not href:
                continue

            lower_href = href.lower()

            if ".pdf" in lower_href:
                return urljoin(
                    base_url,
                    href,
                )

        return None


def test():

    url = (
        "https://www.salute.gov.it/new/it/"
        "ext-avviso-sicurezza-alimentare/"
        "formaggella-1/"
    )

    fetcher = MinistryFetcher()

    recall = fetcher.fetch_recall_page(
        url
    )

    print("=" * 60)
    print("RICHIAMI ITALIA - TEST MINISTERO")
    print("=" * 60)

    print(f"URL: {recall.url}")
    print(f"TITOLO: {recall.title}")
    print(f"PDF: {recall.pdf_url}")

    print()
    print("PRIME RIGHE DELLA PAGINA")
    print("-" * 60)

    lines = [
        line.strip()
        for line in recall.page_text.splitlines()
        if line.strip()
    ]

    for line in lines[:40]:
        print(line)


if __name__ == "__main__":
    test()
