#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lightweight dictionary-based i18n — deliberately not Flask-Babel (that pulls
in a .po/.mo extraction+compilation workflow that's overkill for one extra
language). Just a flat "English UI string" -> "Polish UI string" dict and a
t() lookup: the dict key IS the string you'd otherwise hardcode, gettext
style, so t('Upload') reads exactly like Jinja's own '{{ t("Upload") }}'
would suggest. t() is registered as a Jinja global (see app.py) and is also
importable directly for flash()/JSON error messages in Python code.

Adding a new UI string: write it as t('Some Label') instead of a bare
literal. If PL has no entry yet it just falls back to the English key
itself — nothing breaks if a translation lags behind a newly-added string,
add the PL entry whenever convenient.

For the JS-heavy interactive viewer, this same PL dict (or the relevant
subset of it) is also serialized to window.I18N_PL / window.I18N_LANG (see
interactive_pdf_viewer.html) with a mirrored JS t() helper, so client-side
strings follow the same key convention as server-rendered ones.
"""
from flask import g

# Keys are the literal English source string used elsewhere in the app.
# Please keep this roughly alphabetized within each section to make merge
# conflicts/duplicate-key mistakes easier to spot.
PL = {
    # ---- nav / chrome ----
    # NOTE: "FootnotesExtractor" is deliberately NOT translated — the
    # product name was renamed to the literal string "FootnotesExtractor"
    # everywhere (see _nav.html, interactive_pdf_viewer.html, and every
    # page <title>), hardcoded rather than routed through t() at all, since
    # a tool/brand name shouldn't change per language.
    'Home': 'Strona główna',
    'Upload': 'Prześlij',
    'History': 'Historia',
    'Collections': 'Kolekcje',
    'Groups': 'Grupy',
    'My Groups': 'Moje grupy',
    'Profile': 'Profil',
    'Admin': 'Administrator',
    'Users': 'Użytkownicy',
    'Models': 'Modele',
    'Logout': 'Wyloguj',
    'Login': 'Zaloguj się',
    'Register': 'Zarejestruj się',
    'Language': 'Język',

    # ---- generic actions / labels ----
    'Save': 'Zapisz',
    'Save Changes': 'Zapisz zmiany',
    'Cancel': 'Anuluj',
    'Delete': 'Usuń',
    'Delete Permanently': 'Usuń trwale',
    'Edit': 'Edytuj',
    'Close': 'Zamknij',
    'Back': 'Wstecz',
    'Create': 'Utwórz',
    'Add': 'Dodaj',
    'Remove': 'Usuń',
    'Search': 'Szukaj',
    'Filename': 'Nazwa pliku',
    'Status': 'Status',
    'Date': 'Data',
    'Actions': 'Akcje',
    'Name': 'Nazwa',
    'Owner': 'Właściciel',
    'Editor': 'Edytor',
    'Viewer': 'Widz',
    'Role': 'Rola',
    'Members': 'Członkowie',
    'Select All': 'Zaznacz wszystko',
    'selected': 'zaznaczono',
    'Nothing here yet.': 'Nic tu jeszcze nie ma.',
    'Yes': 'Tak',
    'No': 'Nie',
    'Confirm': 'Potwierdź',
    'Loading...': 'Wczytywanie...',
    'Optional': 'Opcjonalne',
    'Drag to resize': 'Przeciągnij, aby zmienić rozmiar',

    # ---- login / register ----
    'Username': 'Nazwa użytkownika',
    'Password': 'Hasło',
    'Confirm Password': 'Potwierdź hasło',
    'No account?': 'Nie masz konta?',
    'No account? Ask an administrator to create one for you.': 'Nie masz konta? Poproś administratora o jego utworzenie.',
    'Create the first account': 'Utwórz pierwsze konto',
    'No accounts exist yet — this one becomes the administrator. After that, only admins can create new accounts.':
        'Nie ma jeszcze żadnych kont — to konto stanie się kontem administratora. Później nowe konta będą mogli tworzyć tylko administratorzy.',
    'At least 8 characters.': 'Co najmniej 8 znaków.',
    'Create Account': 'Utwórz konto',
    'Already have an account?': 'Masz już konto?',

    # ---- index / upload page ----
    # Split across 3 t() calls in the template so the Latin term "ibidem" can
    # stay italicized in the middle — 'reference handling' is deliberately
    # translated to '' since the Polish word for "reference handling"
    # ("obsługą odwołań") already appears at the end of the first fragment,
    # reading naturally as "...zaawansowaną obsługą odwołań ibidem".
    'Extract and analyze footnotes with advanced': 'Wyodrębniaj i analizuj przypisy z zaawansowaną obsługą odwołań',
    'ibidem': 'ibidem',
    'reference handling': '',
    'View History': 'Zobacz historię',
    'Click to select PDFs or drag & drop': 'Kliknij, aby wybrać pliki PDF, lub przeciągnij je tutaj',
    'Maximum file size: 900MB per file': 'Maksymalny rozmiar pliku: 900 MB na plik',
    'Advanced Options': 'Opcje zaawansowane',
    'Object Detection Model': 'Model wykrywania obiektów',
    'Disabled by admin': 'Wyłączone przez administratora',
    'NER Model Suite': 'Zestaw modeli NER',
    'OCR Engine': 'Silnik OCR',
    'Footnote Splitting': 'Dzielenie przypisów',
    'Split regions by footnote marker': 'Dziel obszary według znacznika przypisu',
    'Splitting assumes footnote markers run contiguously through the document. Turn it off for PDFs built from non-adjacent pages, where the marker sequence jumps.':
        'Dzielenie zakłada, że znaczniki przypisów przebiegają w dokumencie w sposób ciągły. Wyłącz tę opcję dla plików PDF złożonych z niesąsiadujących stron, gdzie kolejność znaczników się urywa.',
    'OD Detection Confidence': 'Pewność wykrywania OD',
    'Reference Confidence': 'Pewność odwołań',
    'Component Confidence': 'Pewność komponentów',
    'Batch Size': 'Rozmiar wsadu',
    'Slower, Less Memory': 'Wolniej, mniej pamięci',
    'Recommended': 'Zalecane',
    'Faster, More Memory': 'Szybciej, więcej pamięci',
    'Fastest, High Memory': 'Najszybciej, dużo pamięci',
    'Debug Mode': 'Tryb debugowania',
    'Include detailed statistics': 'Uwzględnij szczegółowe statystyki',
    'Force CPU Processing': 'Wymuś przetwarzanie na CPU',
    'Use if GPU issues occur': 'Użyj w razie problemów z GPU',
    'Enable GROBID': 'Włącz GROBID',
    'IN DEVELOPMENT': 'W TRAKCIE PRAC',
    'Extract bibliography/reference page via GROBID': 'Wyodrębnij stronę bibliografii/odwołań za pomocą GROBID',
    'Enable Metadata Enrichment': 'Włącz wzbogacanie metadanych',
    'Enrich references with Google Books, Crossref, OpenAIRE': 'Wzbogać odwołania danymi z Google Books, Crossref, OpenAIRE',
    'Ibidem Handling': 'Obsługa ibidem',
    'Split references holding several works': 'Dziel odwołania zawierające kilka prac',
    'e.g. “Martines 2009, 2012” becomes two references; the later one carries over the author': 'np. „Martines 2009, 2012” staje się dwoma odwołaniami; drugie przejmuje autora',
    'Infer missing components (author, title, etc.) for ibidem/opus citatum references from the matching earlier reference':
        'Uzupełniaj brakujące komponenty (autor, tytuł itd.) dla odwołań ibidem/opus citatum na podstawie odpowiadającego wcześniejszego odwołania',
    'GROBID Server URL': 'Adres URL serwera GROBID',
    'Add to collection': 'Dodaj do kolekcji',
    'None': 'Brak',
    'shared': 'udostępniona',
    '+ New collection...': '+ Nowa kolekcja...',
    'Collections marked "(shared)" are shared with a group — you can change this later from the collection\'s page.':
        'Kolekcje oznaczone jako „(udostępniona)” są udostępnione grupie — możesz to później zmienić na stronie kolekcji.',
    'Extract Footnotes': 'Wyodrębnij przypisy',
    'files selected': 'wybranych plików',
    'MB total': 'MB łącznie',
    'Name for the new collection:': 'Nazwa nowej kolekcji:',
    'Could not create collection:': 'Nie udało się utworzyć kolekcji:',
    'Processing...': 'Przetwarzanie...',

    # ---- status page ----
    'Processing Status': 'Status przetwarzania',
    'Job ID:': 'ID zadania:',
    'Initializing...': 'Inicjalizacja...',
    'Footnotes': 'Przypisy',
    'References': 'Odwołania',
    'Components': 'Komponenty',
    'Ibidem': 'Ibidem',
    'Inferred': 'Wywnioskowane',
    'Time': 'Czas',
    'Interactive Viewer': 'Widok interaktywny',
    'Structured Data': 'Dane strukturalne',
    'Download JSON': 'Pobierz JSON',
    'Error Details': 'Szczegóły błędu',
    'Upload Another PDF': 'Prześlij kolejny PDF',
    'Processing Log': 'Dziennik przetwarzania',
    'Elapsed time:': 'Czas, jaki upłynął:',
    'Processing time:': 'Czas przetwarzania:',
    'Processing Completed!': 'Przetwarzanie zakończone!',
    'Processing Failed': 'Przetwarzanie nie powiodło się',
    'Waiting in queue...': 'Oczekiwanie w kolejce...',

    # ---- results page ----
    'Results': 'Wyniki',
    'Processing Results': 'Wyniki przetwarzania',
    'Summary Statistics': 'Podsumowanie statystyk',
    'Processing Time': 'Czas przetwarzania',
    'Structured View': 'Widok strukturalny',
    'Raw JSON': 'Surowy JSON',
    'Footnote': 'Przypis',
    'Original Text:': 'Tekst oryginalny:',
    'Extracted References': 'Wyodrębnione odwołania',
    'Reference': 'Odwołanie',
    'Manual': 'Ręczne',
    'confidence': 'pewności',
    'Full Citation:': 'Pełne cytowanie:',
    'Enriched Metadata': 'Wzbogacone metadane',
    'Bibliography References (GROBID)': 'Odwołania bibliograficzne (GROBID)',
    'Raw JSON Data': 'Surowe dane JSON',
    'Copy JSON': 'Kopiuj JSON',
    'Open Interactive Viewer': 'Otwórz widok interaktywny',
    'Export': 'Eksportuj',
    'BibTeX - Standard': 'BibTeX – standardowy',
    'BibTeX - Expanded': 'BibTeX – rozszerzony',
    'Formatted Styles': 'Style sformatowane',
    'APA 7th Edition': 'APA, wydanie 7',
    'Chicago 17th Edition': 'Chicago, wydanie 17',
    'MLA 9th Edition': 'MLA, wydanie 9',
    'Other': 'Inne',
    'Reference Strings': 'Teksty Referencji',
    'Upload New PDF': 'Prześlij nowy PDF',
    'Copied!': 'Skopiowano!',
    'Failed to copy JSON to clipboard': 'Nie udało się skopiować JSON do schowka',

    # ---- profile page ----
    'My Profile': 'Mój profil',
    'Account details, password, and API keys for': 'Dane konta, hasło i klucze API dla',
    "New API key created — copy it now, it won't be shown again:": 'Utworzono nowy klucz API — skopiuj go teraz, nie zostanie ponownie wyświetlony:',
    'Profile Info': 'Informacje o profilu',
    'First name': 'Imię',
    'Last name': 'Nazwisko',
    'optional': 'opcjonalne',
    'E-mail address': 'Adres e-mail',
    'Change Password': 'Zmień hasło',
    'Current password': 'Obecne hasło',
    'New password': 'Nowe hasło',
    'Confirm new password': 'Potwierdź nowe hasło',
    'Update': 'Zaktualizuj',
    'API Keys': 'Klucze API',
    "Use an API key to authenticate scripts against this app's JSON API instead of logging in with a browser. Send it as either":
        'Użyj klucza API, aby uwierzytelniać skrypty względem API JSON tej aplikacji zamiast logować się przez przeglądarkę. Wyślij go jako',
    'or': 'lub',
    'What\'s this key for? (e.g. \'upload script\')': 'Do czego służy ten klucz? (np. „skrypt przesyłający”)',
    'Generate': 'Wygeneruj',
    'Name': 'Nazwa',
    'Key': 'Klucz',
    'Created': 'Utworzono',
    'Last used': 'Ostatnio użyto',
    'Never': 'Nigdy',
    'Revoked': 'Unieważniony',
    "Revoke the key '{name}'? Anything using it will stop working immediately.": 'Unieważnić klucz „{name}”? Wszystko, co go używa, natychmiast przestanie działać.',
    'Revoke': 'Unieważnij',
    "You haven't generated any API keys yet.": 'Nie wygenerowano jeszcze żadnych kluczy API.',

    # ---- groups list page ----
    'Create a group to share processed publications and their edits with others.':
        'Utwórz grupę, aby udostępniać przetworzone publikacje i ich edycje innym osobom.',
    'Create a Group': 'Utwórz grupę',
    'Group name': 'Nazwa grupy',
    'Create': 'Utwórz',
    'Your Groups': 'Twoje grupy',
    "You're not in any groups yet.": 'Nie należysz jeszcze do żadnej grupy.',

    # ---- group detail page ----
    'Joined': 'Dołączył(a)',
    'Site-wide admin': 'Administrator całej witryny',
    'Owners can manage membership and permissions (and edit/delete shared jobs); editors can edit and delete jobs shared with this group; viewers can only browse them.':
        'Właściciele mogą zarządzać członkostwem i uprawnieniami (oraz edytować/usuwać udostępnione zadania); edytorzy mogą edytować i usuwać zadania udostępnione tej grupie; widzowie mogą je jedynie przeglądać.',
    "This group's only owner — promote another member to owner first to change this":
        'Jedyny właściciel tej grupy — najpierw awansuj innego członka na właściciela, aby to zmienić',
    "This group's only owner — promote another member to owner first to remove them":
        'Jedyny właściciel tej grupy — najpierw awansuj innego członka na właściciela, aby go usunąć',
    'Remove {name} from this group?': 'Usunąć {name} z tej grupy?',
    'Username to add': 'Nazwa użytkownika do dodania',
    'Add Member': 'Dodaj członka',
    'Shared Collections': 'Udostępnione kolekcje',
    'Only collections are shared with a group — add publications to a shared collection (or share one of your own collections here) to give this group access.':
        'Grupie udostępniane są tylko kolekcje — dodaj publikacje do udostępnionej kolekcji (lub udostępnij tu jedną ze swoich kolekcji), aby dać tej grupie dostęp.',
    'No collections have been shared with this group yet. Share one from its own page.':
        'Żadna kolekcja nie została jeszcze udostępniona tej grupie. Udostępnij ją z jej własnej strony.',

    # ---- collections list page ----
    'My Collections': 'Moje kolekcje',
    'My Publications': 'Moje publikacje',
    'Every upload, organized into collections — "User\'s Uploads" always has everything not filed elsewhere; group them further below.':
        'Każdy przesłany plik uporządkowany w kolekcje — „Pliki użytkownika” zawsze zawierają wszystko, co nie zostało przypisane gdzie indziej; grupuj je dalej poniżej.',
    "Nothing here yet — every upload that isn't filed into another collection shows up here automatically.":
        'Nic tu jeszcze nie ma — każdy przesłany plik, który nie trafił do innej kolekcji, pojawia się tu automatycznie.',
    'Create a Collection': 'Utwórz kolekcję',
    'Collection name': 'Nazwa kolekcji',
    'Short description (optional)': 'Krótki opis (opcjonalnie)',
    'Private': 'Prywatna',
    'Your Collections': 'Twoje kolekcje',
    "You don't have any other collections yet.": 'Nie masz jeszcze żadnych innych kolekcji.',
    "User's Uploads": 'Pliki użytkownika',
    'default': 'domyślna',

    # ---- collection detail page ----
    'Rename / Edit Description': 'Zmień nazwę / edytuj opis',
    'Sharing': 'Udostępnianie',
    'Update Sharing': 'Zaktualizuj udostępnianie',
    'Delete this collection? Jobs inside it are not deleted, only removed from the collection.':
        'Usunąć tę kolekcję? Zadania w niej zawarte nie zostaną usunięte, tylko wyjęte z kolekcji.',
    'Delete Collection': 'Usuń kolekcję',
    "This is your default Uploads collection and can't be deleted.": 'To jest Twoja domyślna kolekcja Plików i nie można jej usunąć.',
    'Publications in this Collection': 'Publikacje w tej kolekcji',
    'No publications in this collection yet.': 'Brak publikacji w tej kolekcji.',

    # ---- admin users page ----
    'Admin — Users': 'Administrator — użytkownicy',
    'Create account': 'Utwórz konto',
    'Confirm password': 'Potwierdź hasło',
    'The account is created with this password immediately — share it with the user out of band; they can change it once logged in.':
        'Konto zostaje od razu utworzone z tym hasłem — przekaż je użytkownikowi innym kanałem; będzie mógł je zmienić po zalogowaniu.',
    'you': 'Ty',
    'Delete user {name}? This cannot be undone.': 'Usunąć użytkownika {name}? Tej operacji nie można cofnąć.',

    # ---- admin models page ----
    'Admin — Models': 'Administrator — modele',
    "Turn a model off to hide it (greyed out, not selectable) from the upload form for everyone — the checkpoint/suite stays on disk either way, this only controls whether it's offered. Already-completed jobs that used a now-disabled model are unaffected.":
        'Wyłącz model, aby ukryć go (wyszarzony, niewybieralny) w formularzu przesyłania dla wszystkich — punkt kontrolny/zestaw i tak pozostaje na dysku, to ustawienie kontroluje jedynie, czy jest oferowany. Już ukończone zadania, które użyły teraz wyłączonego modelu, pozostają bez zmian.',
    'On': 'Włączony',
    'Model': 'Model',
    'Description': 'Opis',
    'Disabled': 'Wyłączony',
    'None found on disk.': 'Nie znaleziono żadnego na dysku.',
    'Object Detection Models': 'Modele wykrywania obiektów',
    'NER Model Suites': 'Zestawy modeli NER',
    'OCR Engines': 'Silniki OCR',

    # ---- _job_list.html partial ----
    'Add to Collection': 'Dodaj do kolekcji',
    'Remove from Group': 'Usuń z grupy',
    'Remove from Collection': 'Usuń z kolekcji',
    'Export Citations': 'Eksportuj cytowania',
    'Verified by {who}': 'Zweryfikowane przez {who}',
    'unknown user': 'nieznanego użytkownika',
    'on {date}': '{date}',
    'by': 'autor:',
    'Journal:': 'Czasopismo:',
    'ISBN:': 'ISBN:',
    'ISSN:': 'ISSN:',
    'DOI:': 'DOI:',
    'License:': 'Licencja:',
    # Job status values (job.status — 'completed'/'failed'/'processing'/'queued'/'error')
    'completed': 'ukończone',
    'failed': 'nieudane',
    'processing': 'przetwarzanie',
    'queued': 'w kolejce',
    'error': 'błąd',

    # ---- _job_list_scripts.html (bulk actions) ----
    'skipped': 'pominięto',
    '(no permission, or already gone)': '(brak uprawnień lub już nie istnieje)',
    'Permanently delete {n} job(s)? This cannot be undone.': 'Trwale usunąć {n} zadanie(a)? Tej operacji nie można cofnąć.',
    'Deleted': 'Usunięto',
    'Bulk delete failed:': 'Zbiorcze usuwanie nie powiodło się:',
    'Pick a collection first': 'Najpierw wybierz kolekcję',
    'Added': 'Dodano',
    'Added {n} job(s) to the collection.': 'Dodano {n} zadanie(a) do kolekcji.',
    'Bulk add-to-collection failed:': 'Zbiorcze dodawanie do kolekcji nie powiodło się:',
    "Remove {n} job(s) from this group's sharing? The jobs themselves won't be deleted.":
        'Usunąć {n} zadanie(a) z udostępniania tej grupie? Same zadania nie zostaną usunięte.',
    'Unshared': 'Cofnięto udostępnianie',
    'Bulk unshare failed:': 'Zbiorcze cofanie udostępniania nie powiodło się:',
    "Remove {n} publication(s) from this collection? They won't be deleted, and stay in any other collection they're in.":
        'Usunąć {n} publikację(e) z tej kolekcji? Nie zostaną usunięte i pozostaną w innych kolekcjach, w których się znajdują.',
    'Removed': 'Usunięto',
    'Bulk remove-from-collection failed:': 'Zbiorcze usuwanie z kolekcji nie powiodło się:',
    'Request failed ({status})': 'Żądanie nie powiodło się ({status})',

    # ---- interactive viewer: static chrome ----
    'Interactive PDF Viewer': 'Widok interaktywny PDF',
    'Enhanced Footnote Analysis': 'Zaawansowana analiza przypisów',
    'Back to {name}': 'Powrót do: {name}',
    'Zoom:': 'Powiększenie:',
    'Undo (Ctrl+Z)': 'Cofnij (Ctrl+Z)',
    'Redo (Ctrl+Y)': 'Ponów (Ctrl+Y)',
    'Previous': 'Poprzednia',
    'Page': 'Strona',
    'of': 'z',
    'Next': 'Następna',
    'Which NER model future edits on this job (reclassification, split, merge, reference add/resize) use':
        'Którego modelu NER używają przyszłe edycje tego zadania (reklasyfikacja, dzielenie, scalanie, dodawanie/zmiana rozmiaru odwołania)',
    'NER:': 'NER:',
    'Which OCR engine future box add/resize edits on this job use':
        'Którego silnika OCR używają przyszłe edycje dodawania/zmiany rozmiaru ramek tego zadania',
    'OCR:': 'OCR:',
    'Draw a new footnote bounding box': 'Narysuj nową ramkę przypisu',
    'Add Footnote Box': 'Dodaj ramkę przypisu',
    'Metadata': 'Metadane',
    'Show Footnotes': 'Pokaż przypisy',
    'Show Confidence': 'Pokaż pewność',
    'Show Suggestions': 'Pokaż sugestie',
    'Model vs Reviewed': 'Model a wersja zweryfikowana',
    'Layout Assist': 'Asystent Układu',
    'Layout Assist: time on a page': 'Asystent Układu: czas na stronie',
    'How long a page has to stay on your screen before Layout Assist counts it as looked at. The timers are learned from the pages you approve, or you can set them yourself.': 'Jak długo strona musi pozostać na ekranie, zanim Asystent Układu uzna ją za przejrzaną. Czasy są wyuczone ze stron, które zatwierdzasz, albo możesz ustawić je samodzielnie.',
    'Use values learned from my activity': 'Używaj wartości wyuczonych z mojej aktywności',
    'Pages with nothing detected': 'Strony bez wykrytych elementów',
    'Pages with footnotes': 'Strony z przypisami',
    'learned from your {n} approvals': 'wyuczone z {n} Twoich zatwierdzeń',
    'Grey line on a bar: the standard value. Blue line: your learned value.': 'Szara kreska na pasku: wartość standardowa. Niebieska kreska: Twoja wyuczona wartość.',
    'not enough approvals yet ({n} of {needed}), standard timers': 'za mało zatwierdzeń ({n} z {needed}), czasy standardowe',
    'Page with nothing detected': 'Strona bez wykrytych elementów',
    'Page with footnotes: base time': 'Strona z przypisami: czas bazowy',
    '+ per footnote': '+ za każdy przypis',
    '+ per 100 characters of text': '+ za każde 100 znaków tekstu',
    '+ per reference': '+ za każde odwołanie',
    '+ per labelled part': '+ za każdy oznaczony fragment',
    'Never shorter than': 'Nigdy krócej niż',
    'Never longer than': 'Nigdy dłużej niż',
    'Set to standard timers': 'Ustaw czasy standardowe',
    'Set to learned timers': 'Ustaw czasy wyuczone',
    'Could not save': 'Nie udało się zapisać',
    'With these timers: a page with nothing detected after {a}, a typical page (3 footnotes, 600 characters, 4 references, 20 parts) after {b}.': 'Przy tych czasach: strona bez wykrytych elementów po {a}, typowa strona (3 przypisy, 600 znaków, 4 odwołania, 20 fragmentów) po {b}.',
    'Your own timers: learned from the pages you approve, or set on your profile.': 'Twoje własne czasy: wyuczone ze stron, które zatwierdzasz, albo ustawione w profilu.',
    'Layout Assist settings saved': 'Zapisano ustawienia Asystenta Układu',
    'Please enter a number': 'Wpisz liczbę',
    "Learns this book's footnote layout and citation style from the document and from your edits: flags boxes and labels worth checking, suggests footnotes the detector missed, and carries your confirmations over to pages you haven't approved yet":
        'Uczy się układu przypisów i stylu cytowania tej książki z dokumentu i Twoich poprawek: wskazuje ramki i etykiety warte sprawdzenia, podsuwa przypisy pominięte przez detektor i przenosi Twoje potwierdzenia na strony, których jeszcze nie zatwierdzono',
    'Layout Consistency Prior': 'Spójność układu strony',
    'Citation Style Consistency': 'Spójność stylu cytowania',
    "Check labels against this document's own citation style":
        'Sprawdzaj etykiety wzgledem stylu cytowania tego dokumentu',
    'Use page-to-page layout consistency': 'Wykorzystaj spójność układu między stronami',
    'Show References': 'Pokaż odwołania',
    'All fields optional.': 'Wszystkie pola są opcjonalne.',
    'Authors': 'Autorzy',
    'e.g. Jane Doe, John Smith': 'np. Jan Kowalski, Anna Nowak',
    'Author name(s), comma-separated': 'Nazwisko(a) autora/autorów, oddzielone przecinkami',
    'Title': 'Tytuł',
    "The book or publication's title": 'Tytuł książki lub publikacji',
    'Year': 'Rok',
    'Year of publication': 'Rok publikacji',
    'Publisher': 'Wydawca',
    'Publishing house or organization': 'Wydawnictwo lub organizacja',
    'Journal': 'Czasopismo',
    'Journal or series name, if this is a journal article rather than a standalone book':
        'Nazwa czasopisma lub serii, jeśli jest to artykuł, a nie samodzielna książka',
    'International Standard Book Number': 'Międzynarodowy Znormalizowany Numer Książki',
    'International Standard Serial Number, for journals/series': 'Międzynarodowy Znormalizowany Numer Wydawnictwa Ciągłego, dla czasopism/serii',
    'Digital Object Identifier, e.g. 10.1000/xyz123': 'Cyfrowy identyfikator obiektu, np. 10.1000/xyz123',
    'License': 'Licencja',
    'e.g. CC BY 4.0': 'np. CC BY 4.0',
    'License this work is published under': 'Licencja, na jakiej opublikowano tę pracę',
    'Saved': 'Zapisano',
    'Loading PDF...': 'Wczytywanie PDF...',
    'Label to Apply': 'Etykieta do zastosowania',
    'Annotate': 'Oznaczanie',
    'Footnote box': 'Ramka przypisu',
    'Models used for edits': 'Modele używane przy edycji',
    'Footnote References': 'Referencje z przypisów',
    'Show the Footnote References': 'Pokaż referencje z przypisów',
    'Without an entry': 'Bez pozycji',
    'Footnote references with no entry in the Reference List': 'Referencje z przypisów bez pozycji w bibliografii',
    'Without ibid./op. cit.': 'Bez ibid./op. cit.',
    'Leave out the references that only point back: ibid., op. cit. (an idem with a title of its own stays)': 'Pomiń referencje, które tylko odsyłają wstecz: ibid., op. cit. (idem z własnym tytułem zostaje)',
    'One per work': 'Jedna na pracę',
    'Each work once, with every place it is cited (as the exports)': 'Każda praca raz, ze wszystkimi miejscami cytowania (jak w eksporcie)',
    'Search: the references and the footnotes’ text': 'Szukaj: w referencjach i w tekście przypisów',
    'Search the footnote references': 'Przeszukaj referencje z przypisów',
    'Reference list: {k} of {n} entries linked to footnote references': 'Bibliografia: {k} z {n} pozycji połączonych z referencjami z przypisów',
    'links being counted, reload to see them': 'połączenia są liczone, odśwież, aby je zobaczyć',
    'hold while finishing an edit to move the boundary only — nothing below it is re-read or re-tagged':
        'przytrzymaj przy zatwierdzaniu zmiany, aby przesunąć tylko granicę — nic poniżej nie jest ponownie odczytywane ani tagowane',
    'Footnote Predictions': 'Predykcje przypisu',
    'Reference Page Predictions': 'Predykcje bibliografii',
    'Instances': 'Instancje',
    'Has a reference list ({n} entries)': 'Ma bibliografię (pozycje: {n})',
    '{n} item(s) marked TO REVIEW': 'Oznaczone DO PRZEJRZENIA: {n}',
    'Hide or show the labels (their keyboard keys keep working)': 'Ukryj lub pokaż etykiety (ich klawisze nadal działają)',
    'How strictly footnote references are matched to the list': 'Jak ściśle odwołania z przypisów są dopasowywane do listy',
    'Titles at least 80% alike; author and year only when a title is missing': 'Tytuły podobne co najmniej w 80%; autor i rok tylko przy braku tytułu',
    'Also a short title found inside the longer one, and author and year with titles at least half alike': 'Także krótki tytuł zawarty w dłuższym oraz autor i rok przy tytułach podobnych co najmniej w połowie',
    'Also author and year alone, and the title found anywhere in the entry': 'Także sam autor i rok oraz tytuł znaleziony w dowolnym miejscu pozycji',
    'Strict': 'Ścisłe',
    'Balanced': 'Zrównoważone',
    'Lenient': 'Łagodne',
    'Show or hide the Reference List column': 'Pokaż lub ukryj kolumnę Bibliografia',
    "Find each book's own reference list and link it to the footnotes (in the background)": 'Znajdź bibliografię każdej książki i połącz ją z przypisami (w tle)',
    'Link reference lists': 'Połącz bibliografie',
    'Reference lists queued for {n} document(s); {s} skipped (not finished processing). Each viewer shows the result once it is done.': 'Bibliografie w kolejce dla dokumentów: {n}; pominięte (przetwarzanie niezakończone): {s}. Wynik pojawi się w przeglądarce każdego dokumentu.',
    'Reference List': 'Bibliografia',
    'Reload the links (after editing footnotes)': 'Odśwież połączenia (po edycji przypisów)',
    'Download the links as CSV': 'Pobierz połączenia jako CSV',
    'Set list': 'Ustaw listę',
    'Paste BibTeX': 'Wklej BibTeX',
    'Upload .bib': 'Prześlij .bib',
    "From the book's own reference list": 'Z bibliografii samej książki',
    'All': 'Wszystkie',
    'Linked': 'Połączone',
    'Unlinked': 'Niepołączone',
    'Footnotes without an entry': 'Przypisy bez pozycji',
    'Reference list as BibTeX': 'Bibliografia jako BibTeX',
    "Paste the entries (e.g. the PolIndex records of this article). Each entry's note, if it has one, is shown as its text. Setting a list replaces the current one; decisions on entries that remain are kept.": 'Wklej pozycje (np. rekordy PolIndex tego artykułu). Pole note pozycji, jeśli jest, pokazywane jest jako jej tekst. Ustawienie listy zastępuje obecną; decyzje dla pozycji, które pozostają, są zachowane.',
    'Link to a list entry': 'Połącz z pozycją listy',
    'Filter entries': 'Filtruj pozycje',
    'Reference List Linker': 'Łączenie bibliografii',
    'To Review': 'Do przejrzenia',
    'To review': 'Do przejrzenia',
    'Search: author, title, year, any words': 'Szukaj: autor, tytuł, rok, dowolne słowa',
    'Search the reference list': 'Przeszukaj bibliografię',
    'Search footnotes': 'Szukaj w przypisach',
    '{n} publications · {v} verified': 'publikacje: {n} · zweryfikowane: {v}',
    'What does it refer to?': 'Do czego się odnosi?',
    'Filter the references before it': 'Filtruj wcześniejsze referencje',
    'Show the footnote search': 'Pokaż wyszukiwanie w przypisach',
    'Show the Reference List': 'Pokaż bibliografię',
    'Minimize': 'Zwiń',
    'Words in the footnotes and their references': 'Słowa w przypisach i ich referencjach',
    'Go to the next item marked TO REVIEW': 'Przejdź do następnego elementu oznaczonego DO PRZEJRZENIA',
    'A small button in the viewer that marks a footnote reference, or its link to the reference list, "TO REVIEW", to come back to it later; the marked items are counted and can be stepped through.': 'Mały przycisk w przeglądarce, który oznacza referencję w przypisie lub jej połączenie z bibliografią jako „DO PRZEJRZENIA”, aby do niej wrócić; oznaczone elementy są liczone i można przechodzić między nimi.',
    'Metadata Enrichment': 'Wzbogacanie metadanych',
    'Features': 'Funkcje',
    'on': 'wł.',
    'off': 'wył.',
    'Invalid feature': 'Nieprawidłowa funkcja',
    '{feature} is not enabled for your account': 'Funkcja {feature} nie jest włączona dla Twojego konta',
    '{feature} for {name}: {state}': '{feature} dla {name}: {state}',
    'switched off, a feature is hidden from that user completely.': 'wyłączona funkcja jest całkowicie ukryta przed danym użytkownikiem.',
    "Find the book's reference list": 'Znajdź bibliografię książki',
    'Looks for the bibliography printed in the book and shows it in the viewer': 'Wyszukuje bibliografię wydrukowaną w książce i pokazuje ją w przeglądarce',
    "Layout suggestions, consistency checks and page approvals in the viewer, and the page timers on the profile. When off it still learns from the user's work, but shows nothing and changes nothing on its own.": 'Sugestie układu, kontrole spójności i zatwierdzanie stron w przeglądarce oraz liczniki czasu w profilu. Wyłączony nadal uczy się z pracy użytkownika, ale niczego nie pokazuje ani sam nie zmienia.',
    'The reference list found inside a book: the option to look for it at upload, and the Reference Page Predictions box and outlines in the viewer.': 'Bibliografia znaleziona w samej książce: opcja jej wyszukania przy przesyłaniu oraz ramka Predykcje bibliografii i obrysy w przeglądarce.',
    "The book's reference list (its own or one given as BibTeX) linked to the references in its footnotes, with links to confirm or reject in the viewer.": 'Bibliografia książki (własna lub podana jako BibTeX) połączona z odwołaniami w przypisach; połączenia można potwierdzać i odrzucać w przeglądarce.',
    'Looking references up in external catalogues (in development).': 'Wyszukiwanie odwołań w zewnętrznych katalogach (w przygotowaniu).',
    'Choosing models, thresholds and processing options at upload and in the viewer. When off, uploads - also through the API - use the defaults.': 'Wybór modeli, progów i opcji przetwarzania przy przesyłaniu i w przeglądarce. Wyłączone: przesyłane pliki — także przez API — używają ustawień domyślnych.',
    'Without a licence NVIDIA throttles the GPU about a hundredfold.': 'Bez licencji NVIDIA spowalnia GPU około stukrotnie.',
    'GPU licence: {s}': 'Licencja GPU: {s}',
    'Restart pending: finishing the documents in progress': 'Zaplanowany restart: kończenie przetwarzanych dokumentów',
    'nothing to load': 'nic do wczytania',
    'Parallel processing': 'Przetwarzanie równoległe',
    '{n} job worker(s)': 'Wątki przetwarzania: {n}',
    '{n} document(s) processing now': 'Dokumenty przetwarzane teraz: {n}',
    'GPU memory: {used} of {total} GB in use': 'Pamięć GPU: {used} z {total} GB w użyciu',
    "Instances is how many loaded copies of a model the app keeps. A document being processed uses one copy each of its OCR engine, detection model and NER suite, so with 2 copies of each, two documents are processed at the same time; the app runs as many jobs at once as the highest Instances setting, and a job waits while every copy of a model it needs is in use. Each copy of Surya OCR and of a NER suite takes GPU memory: raise one step at a time and watch the figure above.": "Instancje to liczba wczytanych kopii modelu, które aplikacja utrzymuje. Przetwarzany dokument używa po jednej kopii silnika OCR, modelu detekcji i zestawu NER, więc przy 2 kopiach każdego dwa dokumenty są przetwarzane jednocześnie; aplikacja uruchamia naraz tyle zadań, ile wynosi najwyższe ustawienie Instancji, a zadanie czeka, gdy wszystkie kopie potrzebnego mu modelu są zajęte. Każda kopia Surya OCR i zestawu NER zajmuje pamięć GPU: zwiększaj o jeden krok i obserwuj wartość powyżej.",
    '{key}: {n} instance(s)': '{key}: instancje — {n}',
    'Bibliography References': 'Odwołania bibliograficzne',
    'Edit Footnote': 'Edytuj przypis',
    'Edit the text directly, click in it to place a split marker (or use a suggested marker below the text), or merge with an adjacent piece. Nothing here is saved until "Save Changes".':
        'Edytuj tekst bezpośrednio, kliknij w nim, aby umieścić znacznik podziału (lub użyj sugerowanego znacznika poniżej tekstu), albo scal z sąsiednim fragmentem. Nic tu nie jest zapisywane, dopóki nie klikniesz „Zapisz zmiany”.',
    'Re-reads the changed piece(s) with the reference/component model — splitting and merging above are instant either way. Hold Shift to skip it: a corrected piece keeps the tags it already has, re-aligned onto the new text, while pieces made by splitting or merging stay untagged.':
        'Ponownie odczytuje zmienione fragmenty modelem odwołań/komponentów — dzielenie i scalanie powyżej działa natychmiast w obu przypadkach. Przytrzymaj Shift, aby to pominąć: poprawiony fragment zachowa dotychczasowe tagi, dopasowane do nowego tekstu, a fragmenty powstałe z podziału lub scalenia pozostaną bez tagów.',
    'save without re-running NER': 'zapisz bez ponownego uruchamiania NER',

    # ---- flash messages: auth_routes.py ----
    'Self-registration is disabled. Ask an administrator to create your account.':
        'Samodzielna rejestracja jest wyłączona. Poproś administratora o utworzenie konta.',
    'Username and password are required': 'Nazwa użytkownika i hasło są wymagane',
    'Passwords do not match': 'Hasła nie są zgodne',
    'Password must be at least 8 characters': 'Hasło musi mieć co najmniej 8 znaków',
    'That username is already taken': 'Ta nazwa użytkownika jest już zajęta',
    'Account created': 'Konto utworzone',
    'Too many failed attempts for this account. Try again in a few minutes.':
        'Zbyt wiele nieudanych prób dla tego konta. Spróbuj ponownie za kilka minut.',
    'Invalid username or password': 'Nieprawidłowa nazwa użytkownika lub hasło',
    'Group name is required': 'Nazwa grupy jest wymagana',
    'Group not found': 'Nie znaleziono grupy',
    'You are not a member of that group': 'Nie jesteś członkiem tej grupy',
    'Only a group owner or an admin can add members': 'Tylko właściciel grupy lub administrator może dodawać członków',
    'No user named "{username}"': 'Brak użytkownika o nazwie „{username}”',
    'Added {username} to the group as {role}': 'Dodano {username} do grupy jako {role}',
    'Only a group owner or an admin can change member roles': 'Tylko właściciel grupy lub administrator może zmieniać role członków',
    'Invalid role': 'Nieprawidłowa rola',
    "Can't demote the group's only owner — promote another member to owner first":
        'Nie można zdegradować jedynego właściciela grupy — najpierw awansuj innego członka na właściciela',
    'Role updated': 'Zaktualizowano rolę',
    'Only a group owner or an admin can remove members': 'Tylko właściciel grupy lub administrator może usuwać członków',
    "Can't remove the group's only owner — promote another member to owner first":
        'Nie można usunąć jedynego właściciela grupy — najpierw awansuj innego członka na właściciela',
    'Member removed': 'Usunięto członka',
    'Only a group owner or an admin can delete this group': 'Tylko właściciel grupy lub administrator może usunąć tę grupę',
    'Group deleted': 'Usunięto grupę',
    'Delete Group': 'Usuń grupę',
    'Delete this group? Members are removed, and anything shared with the group (jobs, collections) just becomes private again — nothing else is deleted.':
        'Usunąć tę grupę? Członkowie zostaną usunięci, a wszystko udostępnione grupie (przypisy, kolekcje) ponownie stanie się prywatne — nic więcej nie zostanie usunięte.',
    'Collection name is required': 'Nazwa kolekcji jest wymagana',
    'Invalid group': 'Nieprawidłowa grupa',
    'Collection updated': 'Zaktualizowano kolekcję',
    'Sharing updated': 'Zaktualizowano udostępnianie',
    "The default Uploads collection can't be deleted": 'Domyślnej kolekcji Plików nie można usunąć',
    'Collection deleted': 'Usunięto kolekcję',
    'Job not found': 'Nie znaleziono zadania',
    'Job added to collection': 'Dodano zadanie do kolekcji',
    'Job removed from collection': 'Usunięto zadanie z kolekcji',
    'Profile updated': 'Zaktualizowano profil',
    'Current password is incorrect': 'Obecne hasło jest nieprawidłowe',
    'New passwords do not match': 'Nowe hasła nie są zgodne',
    'New password must be at least 8 characters': 'Nowe hasło musi mieć co najmniej 8 znaków',
    'Password changed': 'Zmieniono hasło',
    'Key name is required': 'Nazwa klucza jest wymagana',
    'API key revoked': 'Unieważniono klucz API',
    'Account "{username}" created': 'Utworzono konto „{username}”',
    'You are the last admin — promote someone else before demoting yourself':
        'Jesteś ostatnim administratorem — awansuj kogoś innego, zanim zdegradujesz siebie',
    'Cannot demote the last remaining admin': 'Nie można zdegradować ostatniego pozostałego administratora',
    'You cannot delete your own account': 'Nie możesz usunąć własnego konta',
    'Cannot delete the last remaining admin': 'Nie można usunąć ostatniego pozostałego administratora',
    'User deleted': 'Usunięto użytkownika',
    'Cannot delete this user — they still own jobs or created groups':
        'Nie można usunąć tego użytkownika — nadal jest właścicielem zadań lub utworzył grupy',
    'Invalid model': 'Nieprawidłowy model',
    'enabled': 'włączony',
    'disabled': 'wyłączony',

    # ---- flash messages: app.py ----
    'No file selected': 'Nie wybrano pliku',
    'Please select valid PDF files': 'Wybierz prawidłowe pliki PDF',
    'Successfully queued {n} files for processing.': 'Pomyślnie umieszczono w kolejce {n} plików do przetworzenia.',
    'Invalid job ID': 'Nieprawidłowy identyfikator zadania',
    'Processing not completed yet': 'Przetwarzanie jeszcze nie zostało ukończone',
    'Result file not found': 'Nie znaleziono pliku wynikowego',
    'Results not found': 'Nie znaleziono wyników',
    'File too large. Maximum size is 900MB.': 'Plik jest zbyt duży. Maksymalny rozmiar to 900 MB.',

    # Error pages (404.html / 500.html via _error_base.html)
    'Page not found': 'Nie znaleziono strony',
    'This address does not exist, or the page has moved.': 'Ten adres nie istnieje lub strona została przeniesiona.',
    'Something went wrong': 'Coś poszło nie tak',
    'The server hit an unexpected problem. Please try again — if it keeps happening, let the team know.':
        'Serwer napotkał nieoczekiwany problem. Spróbuj ponownie — jeśli błąd się powtarza, powiadom zespół.',
    'Back to home page': 'Powrót do strony głównej',
    'Type a page number and press Enter': 'Wpisz numer strony i naciśnij Enter',
    'Page number': 'Numer strony',

    # ---- Layout Assist info page (layout_assist_info.html) ----
    'How Layout Assist works':
        'Jak działa Asystent Układu',
    "Layout Assist learns each book's own footnote layout and citation style — from the book itself and from what you do in the viewer — and uses it to point out what may be wrong. It never changes a page you approved or a box you edited. The numbers on this page are the settings currently in effect.":
        'Asystent Układu uczy się układu przypisów i stylu cytowania każdej książki — z samej książki i z tego, co robisz w przeglądarce — i na tej podstawie wskazuje, co może być błędne. Nigdy nie zmienia zatwierdzonej przez Ciebie strony ani poprawionej ramki. Liczby na tej stronie to aktualnie obowiązujące ustawienia.',
    'Footnote boxes':
        'Ramki przypisów',
    'What it looks at, for every detected box:':
        'Co sprawdza dla każdej wykrytej ramki:',
    'Left edge':
        'Lewa krawędź',
    "Where the book's footnote column starts, learned separately for odd and even pages. The strongest signal, and it works for small footnotes too.":
        'Gdzie zaczyna się kolumna przypisów w tej książce — osobno dla stron parzystych i nieparzystych. Najsilniejszy sygnał, działa też dla małych przypisów.',
    'Bottom edge':
        'Dolna krawędź',
    'Where footnotes end on the page. A box running below that is always wrong; ending above it is normal for stacked footnotes.':
        'Gdzie kończą się przypisy na stronie. Ramka sięgająca niżej jest zawsze błędna; kończąca się wyżej to norma przy kilku przypisach jeden nad drugim.',
    'Right edge':
        'Prawa krawędź',
    'Whether a box runs past the text column. A short box is never penalised.':
        'Czy ramka wychodzi poza kolumnę tekstu. Krótka ramka nigdy nie jest karana.',
    'Height':
        'Wysokość',
    'Box height against the number of text lines inside it.':
        'Wysokość ramki w stosunku do liczby wierszy tekstu w niej.',
    'Font size':
        'Rozmiar czcionki',
    "Footnote type size against the book's body text, read from the PDF's text layer.":
        'Rozmiar czcionki przypisu w porównaniu z tekstem głównym książki, odczytany z warstwy tekstowej PDF.',
    'Rejections':
        'Odrzucenia',
    'The type size of boxes reviewers deleted in this book.':
        'Rozmiar czcionki ramek usuniętych przez recenzentów w tej książce.',
    'It learns from detector boxes with confidence of at least {c}%, and from your approvals and edits, weighted as below.':
        'Uczy się z ramek detektora o pewności co najmniej {c}% oraz z Twoich zatwierdzeń i poprawek, z wagami podanymi niżej.',
    'What it does:':
        'Co robi:',
    'Check this box':
        'Sprawdź tę ramkę',
    "a box that doesn't fit the book's layout (its confidence would drop below {c}%, or one edge is far off).":
        'ramka niepasująca do układu książki (jej pewność spadłaby poniżej {c}% albo któraś krawędź jest daleko od normy).',
    'Missed footnote?':
        'Pominięty przypis?',
    'a candidate the detector left out (confidence at least {c}%) that fits the layout well (at least {a}% once the layout is taken into account), at most {m} per page.':
        'kandydat pominięty przez detektor (pewność co najmniej {c}%), który dobrze pasuje do układu (co najmniej {a}% po uwzględnieniu układu), najwyżej {m} na stronę.',
    'Box cut short?':
        'Ramka za mała?',
    'such a candidate overlapping a box that is already there: extending the box covers both and reads the text again.':
        'taki kandydat nachodzący na istniejącą ramkę: powiększenie ramki obejmie oba obszary i odczyta tekst ponownie.',
    'Carrying your confirmations over':
        'Przenoszenie Twoich potwierdzeń',
    "after each confirmation it adds the missed footnotes it is now confident about, only on pages you haven't approved. It never reshapes or deletes a box on its own.":
        'po każdym potwierdzeniu dodaje pominięte przypisy, których jest już pewien — tylko na niezatwierdzonych stronach. Nigdy sam nie zmienia kształtu ramki ani jej nie usuwa.',
    'Ghost boxes':
        'Ramki-duchy',
    'where you changed what the detector predicted, the original box stays faintly visible (dashed, washed out).':
        'tam, gdzie zmieniłeś przewidywanie detektora, pierwotna ramka pozostaje słabo widoczna (przerywana, wyblakła).',
    "Books uploaded before the detector's candidates were saved (around 8 September) can only be checked, not added to.":
        'Książki wgrane, zanim zaczęto zapisywać kandydatów detektora (ok. 8 września), mogą być tylko sprawdzane — nic nie zostanie do nich dodane.',
    'Citation labels':
        'Etykiety cytowań',
    'What it looks at, for every labelled part of a reference:':
        'Co sprawdza dla każdego oznaczonego fragmentu odwołania:',
    'Shape':
        'Kształt',
    'The character pattern: 9 = digit, A = capital letter, a = lowercase letter — e.g. "A. Aaa" for an initial and a surname.':
        'Wzór znaków: 9 = cyfra, A = wielka litera, a = mała litera — np. „A. Aaa" dla inicjału i nazwiska.',
    'Word before':
        'Słowo przed',
    'The token right before it, e.g. "s." before page numbers.':
        'Słowo tuż przed nim, np. „s." przed numerami stron.',
    'Word after':
        'Słowo po',
    'The token right after it.':
        'Słowo tuż po nim.',
    'Word before + shape':
        'Słowo przed + kształt',
    'Both together, e.g. a number after "t." is a volume.':
        'Oba naraz, np. liczba po „t." to tom.',
    'Position':
        'Pozycja',
    'Which part of the reference it is (1st, 2nd, …), and roughly where in the reference it sits, each with its shape.':
        'Która to część odwołania (1., 2., …) i mniej więcej gdzie w odwołaniu się znajduje — każde razem z kształtem.',
    'Length':
        'Długość',
    'How many tokens it has, and whether it is a number.':
        'Ile ma słów i czy jest liczbą.',
    'Exact text':
        'Dokładny tekst',
    'The text itself, e.g. "op. cit.".':
        'Sam tekst, np. „op. cit.".',
    'All of it is kept separately per citation type (normal, ibidem, …). It switches on once a book has at least {n} labelled parts in {r} references.':
        'Wszystko liczone jest osobno dla każdego typu cytowania (zwykłe, ibidem, …). Włącza się, gdy książka ma co najmniej {n} oznaczonych fragmentów w {r} odwołaniach.',
    "Suggests a different label when this book's own evidence disagrees with the stored one clearly, and on at least two features. It never relabels anything on its own.":
        'Proponuje inną etykietę, gdy dane z tej książki wyraźnie przeczą zapisanej — i to w co najmniej dwóch cechach. Nigdy sam nie zmienia etykiet.',
    'Labels the viewer does not show (FootnoteMarker) are never suggested.':
        'Etykiety niewidoczne w przeglądarce (FootnoteMarker) nigdy nie są proponowane.',
    "It learns mostly from the model's own labels, so a mistake the model makes consistently across a book can be learned as the norm — keep or change a suggestion to correct it.":
        'Uczy się głównie z etykiet samego modelu, więc błąd powtarzany przez model w całej książce może zostać uznany za normę — zostaw lub zmień sugestię, aby to poprawić.',
    'Approving pages':
        'Zatwierdzanie stron',
    'Not approved':
        'Niezatwierdzona',
    'Looked at':
        'Przejrzana',
    'the page stayed on screen long enough, with nothing left open on it. Counts as a skim.':
        'strona była na ekranie wystarczająco długo i nic nie zostało na niej otwarte. Liczy się jako pobieżne przejrzenie.',
    'Approved by you':
        'Zatwierdzona przez Ciebie',
    'you clicked the round icon in the Layout Assist panel, or pressed S. Everything on the page counts as checked and any open items on it are marked as fine. Click (or S) again to withdraw; Ctrl+Z undoes it.':
        'kliknięto okrągłą ikonę w panelu Asystenta Układu albo naciśnięto S. Wszystko na stronie liczy się jako sprawdzone, a otwarte sugestie są oznaczane jako w porządku. Kliknij (lub S) ponownie, aby wycofać; Ctrl+Z cofa.',
    'Editing an element':
        'Poprawienie elementu',
    'counts for that element only, above a skim or an approval. Layout Assist never changes a page where you edited a box.':
        'liczy się tylko dla tego elementu, wyżej niż przejrzenie czy zatwierdzenie. Asystent Układu nigdy nie zmienia strony, na której poprawiłeś ramkę.',
    'How long a page must stay on screen to count as looked at:':
        'Jak długo strona musi być na ekranie, aby liczyła się jako przejrzana:',
    'Fixed at {ms} ms for every page on this server (testing setting).':
        'Stałe {ms} ms dla każdej strony na tym serwerze (ustawienie testowe).',
    'Nothing detected on the page:':
        'Nic nie wykryto na stronie:',
    'Otherwise:':
        'W przeciwnym razie:',
    'per footnote':
        'na przypis',
    'per 100 characters of text':
        'na 100 znaków tekstu',
    'per reference':
        'na odwołanie',
    'per labelled part':
        'na oznaczony fragment',
    'between {a} and {b} s':
        'od {a} do {b} s',
    'How much each signal counts':
        'Ile waży każdy sygnał',
    "The model's own output":
        'Wynik samego modelu',
    'Page looked at':
        'Strona przejrzana',
    'Page approved by you':
        'Strona zatwierdzona przez Ciebie',
    'Element confirmed ("Box is fine", "Keep …", accepted unchanged)':
        'Element potwierdzony („Ramka jest dobra", „Zostaw …", zaakceptowany bez zmian)',
    'Element added or edited':
        'Element dodany lub poprawiony',
    'Every approval and edit is recorded with how long the page was on screen, what the page held (footnotes, text length, references, labelled parts) and how many elements you touched.':
        'Każde zatwierdzenie i poprawka są zapisywane razem z czasem na stronie, zawartością strony (przypisy, długość tekstu, odwołania, oznaczone fragmenty) i liczbą zmienionych elementów.',
    'The time a page has to stay on screen to count as looked at is linked to your own activity: it is learned from the pages you approve, separately for pages with nothing detected and pages with footnotes, and you can see it or set it yourself on your profile.':
        'Czas, przez jaki strona musi pozostać na ekranie, aby liczyła się jako przejrzana, jest powiązany z Twoją aktywnością: jest wyuczony ze stron, które zatwierdzasz, osobno dla stron bez wykrytych elementów i stron z przypisami, a w profilu możesz go zobaczyć lub ustawić samodzielnie.',
    'The weights in this table are the same for everyone for now, and can later be learned from how people actually review in the same way.':
        'Wagi w tej tabeli są na razie takie same dla wszystkich i później można je wyuczyć w ten sam sposób z tego, jak ludzie faktycznie sprawdzają.',
    'Shortcuts':
        'Skróty',
    'approve the page on screen, or withdraw your approval':
        'zatwierdź widoczną stronę albo wycofaj zatwierdzenie',
    'previous / next page':
        'poprzednia / następna strona',
    'next to a suggestion on the PDF — accept it / mark it as fine':
        'przy sugestii na PDF — przyjmij / oznacz jako w porządku',
    'No footnotes detected':
        'Nie wykryto przypisów',
    'One short footnote':
        'Jeden krótki przypis',
    'A typical page':
        'Typowa strona',
    'A dense page':
        'Gęsta strona',

    # ---- Layout Assist info: consistency checks ----
    'Consistency checks':
        'Kontrole spójności',
    'Rules that hold for every book, following how the verified books are annotated. Each comes with a one-click fix where the fix is certain, "Fix all" for the whole book, and "Fine as is".':
        'Reguły obowiązujące w każdej książce, zgodne ze sposobem oznaczania zweryfikowanych książek. Każda ma poprawkę jednym kliknięciem tam, gdzie poprawka jest pewna, „Popraw wszystkie” dla całej książki oraz „Jest w porządku”.',
    'Issue and Volume hold only the number: "nr 1" → "1", "t. 41" → "41"; a part holding only "nr" moves onto the number.':
        'Numer i tom zawierają tylko liczbę: „nr 1” → „1”, „t. 41” → „41”; fragment zawierający samo „nr” przenoszony jest na liczbę.',
    'Pages leave out "s." / "p."; a page range is one part ("15-43"), a list is one part per page ("272", "281").':
        'Strony bez „s.” / „p.”; zakres stron to jeden fragment („15-43”), lista — osobny fragment na każdą stronę („272”, „281”).',
    'No stray punctuation at the edge of a part ("163." → "163"), and the footnote number never inside one.':
        'Bez zbędnej interpunkcji na brzegu fragmentu („163.” → „163”) i nigdy z numerem przypisu w środku.',
    'A year with a letter ("2005a") is split into year and suffix; things that are not a year, a DOI or a web address, and backwards page ranges, are pointed out.':
        'Rok z literą („2005a”) dzielony jest na rok i sufiks; wskazywane są też fragmenty, które nie są rokiem, DOI ani adresem internetowym, oraz odwrócone zakresy stron.',
    'Measured on the verified books before being switched on; rules that disagreed with them (for example about "op. cit.") are off.':
        'Sprawdzone na zweryfikowanych książkach przed włączeniem; reguły z nimi niezgodne (np. dotyczące „op. cit.”) są wyłączone.',

    # ---- Layout Assist info: span principle and templates ----
    'Where a part starts and ends follows one principle: the smallest span that still holds all of its information. Whitespace and , ; : at its edges never belong to it; a full stop belongs to it only when it closes an abbreviation ("op.cit.", "et al.", "J."), never after a number ("12." → "12"); a short letter token before a number ("pp.", "S.", "Bd.", "nr") is a prefix and stays outside ("pp. 210" → "210").':
        'Początek i koniec fragmentu wyznacza jedna zasada: najmniejszy zakres, który wciąż zawiera całą informację. Spacje oraz , ; : na brzegach nigdy do niego nie należą; kropka należy tylko wtedy, gdy zamyka skrót („op.cit.”, „et al.”, „J.”), nigdy po liczbie („12.” → „12”); krótkie słowo przed liczbą („pp.”, „S.”, „Bd.”, „nr”) to prefiks i zostaje poza fragmentem („pp. 210” → „210”).',
    'Each book also learns its own span rules from what you do: every part is reduced to its label and the shape of its neighbours ("aa." for "pp.", a full stop after a number, …). Where your edits and approvals disagree with the principle, they win — mark "pp. 210" whole as Pages and the rest of that book\'s "pp. N" parts follow. Once a rule has at least {n} of your actions behind it and none against it, it is applied to pages you have not approved on its own (undoable); before that it is a suggestion.':
        'Każda książka uczy się też własnych reguł zakresu z Twoich działań: każdy fragment sprowadzany jest do etykiety i kształtu sąsiadów („aa.” dla „pp.”, kropka po liczbie, …). Gdy Twoje poprawki i zatwierdzenia przeczą zasadzie, wygrywają one — oznacz całe „pp. 210” jako Strony, a pozostałe fragmenty „pp. N” w tej książce pójdą za tym. Gdy reguła ma za sobą co najmniej {n} Twoich działań i żadnego przeciw, jest stosowana samodzielnie na niezatwierdzonych stronach (z możliwością cofnięcia); wcześniej jest sugestią.',
    'Other checks:':
        'Inne kontrole:',
    'A list of pages in one part is split into one part per page ("272, 281"); a range stays whole ("15-43").':
        'Lista stron w jednym fragmencie dzielona jest na osobne fragmenty („272, 281”); zakres zostaje w całości („15-43”).',
}


def t(key: str, **kwargs) -> str:
    """Looks up `key` in the current request's language (g.lang, set by
    auth.load_language) and falls back to the English key itself when
    untranslated, when g.lang isn't 'pl', or when called with no request
    context at all (e.g. a standalone script). kwargs are applied via
    str.format for simple interpolation, e.g. t('Back to {name}', name=...)."""
    try:
        lang = g.lang
    except Exception:
        lang = 'en'
    text = PL.get(key, key) if lang == 'pl' else key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text
