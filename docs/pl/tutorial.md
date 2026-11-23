# Poradnik

*[English version](../en/tutorial.md)*

1. [Wymagania](#1-wymagania)
2. [Instalacja i uruchomienie](#2-instalacja-i-uruchomienie)
3. [Zakładanie kont](#3-zakładanie-kont)
4. [Przetwarzanie dokumentu](#4-przetwarzanie-dokumentu)
5. [Sprawdzanie wyniku](#5-sprawdzanie-wyniku)
6. [Eksport](#6-eksport)
7. [Praca w zespole](#7-praca-w-zespole)
8. [Uruchomienie na serwerze](#8-uruchomienie-na-serwerze)
9. [Gdy coś nie działa](#9-gdy-coś-nie-działa)

## 1. Wymagania

| | Minimum |
|---|---|
| GPU | NVIDIA, 10 GB pamięci |
| CPU | 4 rdzenie |
| RAM | 12 GB |
| Dysk | 6,1 GB na program i pakiety Pythona, 6,7 GB na modele |
| Oprogramowanie | Linux, Python 3.10 |

Instalowana wersja PyTorch wymaga sterownika NVIDIA obsługującego CUDA 12.4. Przed instalacją warto sprawdzić
sterownik poleceniem `nvidia-smi`.

## 2. Instalacja i uruchomienie

```bash
git clone https://github.com/FootnotesExtractor/FootnotesExtractor.git
cd FootnotesExtractor

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python setup_models.py
FOOTNOTES_INSECURE_COOKIES=1 python wsgi.py --host 127.0.0.1
```

Następnie należy otworzyć <http://127.0.0.1:5002>.

- `setup_models.py` pobiera modele wykrywania i analizy (około 5,6 GB). Surya pobiera własne wagi OCR
  (około 1,1 GB) przy pierwszym uruchomieniu.
- `FOOTNOTES_INSECURE_COOKIES=1` pozwala zalogować się przez zwykłe HTTP na własnym komputerze. Tego
  ustawienia nie wolno stosować na serwerze.
- `python setup_models.py --herbert` pobiera dodatkowo drugi zestaw modeli analizy. Zajmuje 4,2 GB na dysku
  i wymaga więcej pamięci GPU niż podane minimum.

## 3. Zakładanie kont

1. Otworzyć `/register`. Program wypisuje w dzienniku jednorazowy **token konfiguracji** i zapisuje go w pliku
   `setup_token.txt` obok `app.db`.
2. Podać token, nazwę użytkownika i hasło o długości co najmniej 8 znaków. To pierwsze konto jest
   administratorem, a rejestracja zostaje zamknięta.
3. Każde kolejne konto zakłada się w **Administrator → Użytkownicy**. Tam też wybiera się funkcje opcjonalne
   widoczne dla danego użytkownika (zob. [Funkcje do włączenia](features.md#8-funkcje-do-włączenia)).

Interfejs można przełączyć na polski w menu języka na górnym pasku.

## 4. Przetwarzanie dokumentu

1. Na stronie głównej wybrać jeden lub kilka plików PDF albo przeciągnąć je na stronę. Każdy plik może mieć do
   200 MB. Jedno przesłanie może obejmować do 900 plików i łącznie 850 MB.
2. Opcjonalnie wybrać kolekcję w polu **Dodaj do kolekcji** lub otworzyć **Opcje zaawansowane**, aby wybrać
   modele, silnik OCR, progi pewności i opcje przetwarzania.
3. Nacisnąć **Wyodrębnij przypisy**. Strona statusu pokazuje postęp. Dokumenty czekają w kolejce, więc kilka
   przesłanych plików jest przetwarzanych po kolei.
4. Po zakończeniu otworzyć **Widok interaktywny**.

## 5. Sprawdzanie wyniku

Widok pokazuje po lewej stronę książki, a obok to, co zostało znalezione.

| Obszar | Do czego służy |
|---|---|
| Widok strony | Strona PDF z ramką wokół każdego przypisu |
| **Oznaczanie** | Etykiety do nadania oraz **Dodaj ramkę przypisu** |
| **Predykcje przypisu** | Tekst, referencje i części przypisów z tej strony |
| **Bibliografia** | Bibliografia połączona z przypisami (funkcja opcjonalna) |
| **Referencje z przypisów** | Wszystkie referencje z dokumentu, z wyszukiwaniem i filtrami |

Najczęstsze poprawki:

- **Przypis został pominięty.** Nacisnąć `B`, narysować ramkę, a przypis zostanie odczytany i oznaczony.
- **Ramka jest błędna.** Kliknąć ją, przesunąć jej krawędzie albo usunąć klawiszem `Delete`.
- **Tekst jest błędny.** Użyć ołówka. To samo okno pozwala podzielić przypis lub połączyć go z sąsiednim.
- **Etykieta jest błędna.** Zaznaczyć fragment w tekście i nacisnąć klawisz właściwej etykiety lub kliknąć ją
  w panelu **Oznaczanie**. Aby oznaczyć nowy tekst, najpierw wybrać etykietę, a potem zaznaczyć tekst.
- ***Ibid.* wskazuje niewłaściwą pracę.** Wybrać właściwą. Referencja przejmie jej autora, tytuł i rok.

### Klawiatura

| Klawisz | Działanie |
|---|---|
| `←` / `A`, `→` / `D` | Poprzednia strona, następna strona |
| `B` | Rysowanie ramki przypisu |
| `S` | Zatwierdzenie strony (przy włączonym Asystencie Układu) |
| `Ctrl+Z`, `Ctrl+Y` lub `Ctrl+Shift+Z` | Cofnij, ponów |
| `Delete` / `Backspace` | Usunięcie wybranej ramki lub fragmentu tekstu |
| `F`, `J` | Reference, Work |
| `1`–`9`, `0` | Author, Editor, Translator, Title, BookTitle, Journal, Volume, Issue, Publisher, Pages |
| `Q` `W` `E` `R` `T` | PublicationPlace, PublicationYear, PublicationYearSuffix, DOI, Webpage |
| `Y` `U` | Ibidem, OpusCitatum |
| `G` `H` | FirstName, LastName |

### Zatwierdzanie i weryfikacja

Przy włączonym **Asystencie Układu** stronę zatwierdza się po jej sprawdzeniu: kliknięciem ikony w panelu
asystenta lub klawiszem `S`. Na zatwierdzonej stronie asystent niczego sam nie zmienia, a otwarte na niej
sugestie zostają zamknięte. Gdy cała książka jest sprawdzona, widok na ostatniej stronie proponuje oznaczenie
jej jako zweryfikowanej. Znacznik pojawia się potem wszędzie, gdzie dokument jest wymieniony.

## 6. Eksport

Służy do tego przycisk **Eksportuj cytowania** w widoku interaktywnym. Można też zaznaczyć kilka dokumentów
w kolekcji i wyeksportować je razem.

| Format | Zawartość |
|---|---|
| BibTeX – standardowy | Jeden wpis na pracę; *ibid.* i *op. cit.* pozostają w zapisanej postaci |
| BibTeX – rozszerzony | Jak wyżej, z *ibid.* i *op. cit.* zastąpionymi pracą, do której się odnoszą |
| APA 7, Chicago 17, MLA 9 | Sformatowana bibliografia jako zwykły tekst |
| Teksty Referencji | Każda referencja tak, jak została wyodrębniona |
| JSON | Wszystko, z położeniem i pewnością |

Każda praca pojawia się raz. Jej notatka wymienia wszystkie miejsca cytowania: stronę PDF, numer przypisu
i referencję w zapisanej postaci.

## 7. Praca w zespole

- **Kolekcja** to nazwany zbiór dokumentów.
- **Grupa** to zbiór użytkowników. Kolekcję udostępnia się grupie.
- Członek grupy jest **widzem** (czyta), **edytorem** (może poprawiać) albo **właścicielem** (zarządza też
  członkami).
- Dokumenty są prywatne, dopóki nie zostaną udostępnione. Administratorzy widzą wszystko.

## 8. Uruchomienie na serwerze

Program należy uruchomić na adresie lokalnym i umieścić przed nim serwer pośredniczący obsługujący TLS:

```bash
python wsgi.py --host 127.0.0.1 --port 5002 --threads 8
```

Katalog `deploy/` zawiera plik Caddyfile, jego wersję z certyfikatem własnym i jednostkę systemd. Za serwerem
pośredniczącym trzeba ustawić `FOOTNOTES_TRUSTED_PROXIES=1`. Pełne kroki opisano (po angielsku) w
[deploy/DEPLOY.md](../../deploy/DEPLOY.md).

Kopie zapasowe powinny obejmować `uploads/`, `results/` i `app.db`. Aktualizacja programu ich nie zmienia.

## 9. Gdy coś nie działa

| Problem | Co sprawdzić |
|---|---|
| Nie można zalogować się na własnym komputerze | Uruchomić z `FOOTNOTES_INSECURE_COOKIES=1` albo użyć HTTPS |
| Rejestracja wymaga tokenu | Jest w dzienniku programu i w pliku `setup_token.txt` |
| Komunikat „Zbyt duże” przy przesyłaniu | 200 MB na plik PDF, 850 MB na całe przesłanie |
| Brak pamięci CUDA | Zamknąć inne programy korzystające z GPU lub zmniejszyć **Rozmiar wsadu** w **Opcjach zaawansowanych** |
| Przetwarzanie jest powolne | Sprawdzić `nvidia-smi`. Przy niezgodności CUDA PyTorch przechodzi na CPU bez komunikatu o błędzie |
| Dokument pozostaje w kolejce | Dokumenty są przetwarzane po kolei. Ruszy po zakończeniu wcześniejszych |
