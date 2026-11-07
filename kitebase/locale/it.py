from kitebase.i18n import register_translations

register_translations('it', {
    # db endpoint — CRUD
    'Table name is required':           'Nome tabella obbligatorio',
    "Table '{name}' not found":         "Tabella '{name}' non trovata",
    "Unsupported method: '{method}'":   "Metodo non supportato: '{method}'",
    'Record with id {id} not found':    'Record con id {id} non trovato',
    'No data provided for creation':    'Nessun dato fornito per la creazione',
    'Record created successfully':      'Record creato con successo',
    'Record ID is required for updates':'ID record obbligatorio per l\'aggiornamento',
    'No data provided for update':      'Nessun dato fornito per l\'aggiornamento',
    'Some values are not valid':        'Alcuni valori non sono validi',
    'A value is required':              'Valore obbligatorio',
    'Not a valid email address':        'Non è un indirizzo email valido',
    'Record updated successfully':      'Record aggiornato con successo',
    'Record ID is required for deletion':'ID record obbligatorio per l\'eliminazione',
    'Record deleted successfully':      'Record eliminato con successo',
    'Query not defined':                'Query non definita',

    # export
    'Export':                           'Esporta',
    '{n} rows':                         '{n} righe',
    '1 row':                            '1 riga',
    'No rows to export':                'Nessuna riga da esportare',
    'No columns to export':             'Nessuna colonna da esportare',
    '{n} rows are more than the {limit} an export can hold: narrow the selection':
        "{n} righe sono più delle {limit} che un'esportazione può contenere: restringi la selezione",
    "Unknown export format: '{format}'": "Formato di esportazione sconosciuto: '{format}'",
    'Text, tab separated (.txt)':       'Testo separato da tabulazioni (.txt)',

    # auth
    'Username and password are required': 'Username e password obbligatori',
    'Invalid credentials':              'Credenziali non valide',
    'Account is inactive':              'Account non attivo',
    'User not authenticated':           'Utente non autenticato',

    # the guide (help.py)
    'Contents':                         'Indice',
    'Commands':                         'Comandi',
    'Fields':                           'Campi',
    'See also':                         'Vedi anche',
    'Inside the form':                  'Dentro la scheda',
    'When you select a row':            'Quando selezioni una riga',
    'Beside the list':                  'Accanto alla lista',
    'Filling in the fields':            'Come si compilano i campi',
    'Choosing from another table':      "Scegliere da un'altra tabella",
    'Type to search and pick with the arrow keys and Enter. '
    '*Search more...*, at the end of the list, opens the whole table.':
        'Scrivi per cercare e scegli con le frecce e Invio. '
        "*Cerca altro...*, in fondo all'elenco, apre tutta la tabella.",
    'Dates':                            'Date',
    'Type the date, or pick it from the calendar.':
        'Scrivi la data, o sceglila dal calendario.',
    'up to {size} MB':                  'fino a {size} MB',
})
