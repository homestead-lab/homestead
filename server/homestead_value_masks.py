"""What a role below the one that may reveal a Secret is shown of values that
can carry a credential outside a Secret.

- An address (a "URL token": a scheme, "://" - or ":/" or ":\\", which
  other parsers accept - and everything up to the next ASCII whitespace)
  loses only what can be a credential, and fails closed:
  - everything before its last "@" is a login: "https://•••@host/path";
  - a parameter (query or fragment) keeps its value only for a harmless
    name (SHOWN_PARAMS: page, sort, q, lang, ...): "?token=•••&page=2";
  - a scheme not on PLAIN_SCHEMES (pbul://, pover://, gotify://, ...)
    shows nothing after "scheme://";
  - a host named for a notification service (NOTIFY_LABELS: ntfy.*,
    gotify.*, apprise.*, hooks.*) shows no path;
  - from a path segment that holds a secret (after /hooks/, /webhooks/,
    /webhook/ or /services/; a bot<digits>: token; an ntfy.sh topic; a word
    a credential goes by; 16 or more characters that look generated) the
    rest is hidden: "https://hooks.slack.com/services/•••";
  - where the address stops being plain (a host that is not a host and
    port, "%", ";", "=", "\\" or a quote in the path, a parameter name that
    is not plain) the rest of the token is hidden.
- A text that is a JSON object or list is decoded first and every string
  in it checked (an escaped "https:\\/\\/" is found too), so an address ends
  where its string ends (an annotation Homestead keeps as JSON round-trips);
  with a credential it is written back compact. A key holding one, or a key
  given twice in JSON holding one, hides the whole text (JSON_HIDDEN).
- A login without a scheme ("//user:pass@host", Go DSNs "user:pass@tcp(",
  "@unix(") loses everything before its last "@" (or last "@tcp(") too.
- An address that starts inside a word ("user:pass@x://h", "a:https://h")
  hides the whole word: what comes before it may be a login.
- Out of scope, as before: a credential that is not in an address
  ("password: x", "Authorization: Bearer ...") and base64 blobs; and
  addresses this cannot see: scheme-less forms other than the two above
  ("user:pass@host:5432/db", "host?token=x", "Server=h;Password=x"), and a
  scheme that does not start with a letter ("1x://": only "x://..." from
  the letter on is seen, so its login is still masked).
- A text longer than MAX_TEXT that holds an address, or holding more than
  MAX_ADDRESSES of them, is hidden whole.
- A copy saved back with a masked text keeps what is stored there, when the
  mask is exactly what the stored text shows (restore(): list items matched
  by their name, LIST_KEYS); any other masked text is refused, so the mask
  is never saved as a value.
- The Helm controller's install and delete pods run `helm` with the chart's
  values, and a chart may print them, so their logs go only to that role.

The role is the one GET /api/resources/reveal declares, read from the route
table so the two cannot drift apart. Everything here is linear in the
length of the text: anyone who can set an annotation or cause an event
writes it."""
import copy
import json
import re

HIDDEN = "•••"
REVEAL = ("GET", "/api/resources/reveal")
# ASCII whitespace: the only thing that ends a URL token.
SPACE = " \t\r\n\f\v"
SPACES = re.compile(r"[ \t\r\n\f\v]+")
# A URL token: a scheme of at most 32 characters (each start is tried in
# bounded time), ":" and one or two slashes or backslashes, then everything
# up to ASCII whitespace or the end of the text.
TOKEN = re.compile(r"(?ia)[a-z][a-z0-9+.-]{0,31}:[\\/]{1,2}[^ \t\r\n\f\v]*")
# What tokens() looks for first, and what a scheme is made of.
COLON = re.compile(r":[\\/]")
SPACE_AT = re.compile(r"[ \t\r\n\f\v]")
SCHEME = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")
SCHEME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
# A word a credential goes by (tokens, keys, passwords, signatures,
# sessions; a harmless word hidden too is the safe side).
SECRET_WORDS = ("pass", "pwd", "secret", "token", "sig", "credential", "auth", "session", "jwt", "bearer", "cookie",
                "private", "hmac", "otp", "ticket", "apikey", "api-key", "api_key", "access-key", "access_key")
SECRET_WORD = re.compile("|".join(SECRET_WORDS), re.I)


def _secret_word(text):
    """Where the first word a credential goes by starts in text, or -1. A
    long text is searched word by word: a regex alternation is many times
    slower there."""
    if len(text) < 256:
        hit = SECRET_WORD.search(text)
        return hit.start() if hit else -1
    text = text.lower()
    found = [at for at in (text.find(word) for word in SECRET_WORDS) if at >= 0]
    return min(found) if found else -1
# The only parameters whose value is shown: names that page, sort, search or
# pick a language, view or build. Every other value is hidden (fails closed:
# a webhook's room, channel or "x" can be its secret as well as a "token").
SHOWN_PARAMS = frozenset(("page", "per_page", "limit", "offset", "sort", "order", "q", "search", "lang", "locale",
                          "theme", "tab", "view", "format", "arch", "ref", "version", "v", "ts"))
# Path segments that hold a secret: everything after a webhook's own
# segment (Slack's /services/, Discord's /webhooks/, /hooks/, /webhook/), a
# Telegram bot token (bot<digits>:...), an ntfy.sh topic (its name is its
# only key), and any long segment that looks generated: 16 or more
# characters mixing letters and digits, hex, or mixed-case base64.
HOOK_SEGMENTS = frozenset(("hooks", "webhooks", "webhook", "services"))
BOT = re.compile(r"(?i)bot[0-9]+:")
TOPIC_HOSTS = frozenset(("ntfy.sh",))
# A host whose first label names a notification service (self-hosted ntfy,
# Gotify, Apprise, a hooks.* receiver): its whole path is hidden, however
# short (a topic or an app token). "/(hidden)" is the outage module's own
# mask (homestead_outage): nothing is left to hide there.
NOTIFY_LABELS = frozenset(("ntfy", "gotify", "apprise", "hooks"))
OWN_MASKS = frozenset(("/(hidden)",))
# Schemes whose addresses are plain host/path addresses. Any other scheme
# (Apprise and shoutrrr notification addresses: pbul://, pover://,
# gotify://, tgram://, ...) can hold a token anywhere, so everything after
# "scheme://" is hidden.
PLAIN_SCHEMES = frozenset((
    "http", "https", "ws", "wss", "ftp", "ftps", "sftp", "git", "ssh", "oci", "file",
    "postgres", "postgresql", "mysql", "mariadb", "mongodb", "mongodb+srv", "redis", "rediss",
    "amqp", "amqps", "mqtt", "mqtts", "nats", "jdbc", "s3", "ldap", "ldaps"))
# Logins without a scheme: "//user:pass@host" and Go DSNs ("user:pass@tcp(",
# "@unix("). A word (up to ASCII whitespace) holding one loses everything
# before its "@".
WORD = re.compile(r"[^ \t\r\n\f\v]+")
DSN = re.compile(r"@(?:tcp6?|udp6?|unix)\(")
LONG = 16
LETTER = re.compile(r"[A-Za-z]")
DIGIT = re.compile(r"[0-9]")
HEX = re.compile(r"[0-9A-Fa-f]+")
BASE64 = re.compile(r"[A-Za-z0-9+_-]+={0,2}")
UPPER, LOWER = re.compile(r"[A-Z]"), re.compile(r"[a-z]")
# The parts of an address shown as they are; anything else starts the hidden rest.
HEAD = re.compile(r"(?ia)([a-z][a-z0-9+.-]{0,31}):[\\/]{1,2}")
HOST = re.compile(r"(?:[A-Za-z0-9._~-]*|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?")
NAME = re.compile(r"[A-Za-z0-9._~\[\]-]*")
VALUE = re.compile(r"[A-Za-z0-9._~+,-]*")
# A path character that is not plain, and the path's secret-holding parts.
PATH_ODD = re.compile(r"[^A-Za-z0-9._~!$'()*+,:/-]")
AFTER_HOOK = re.compile(r"(?i)(?:^|/)(?:" + "|".join(sorted(HOOK_SEGMENTS)) + r")(?=/)")
BOT_SEGMENT = re.compile(r"(?i)(?:^|/)bot[0-9]+:")
LONG_SEGMENT = re.compile(r"[^/]{16,}")
HOST_END = re.compile(r"[/?#\\]")
QUERY = re.compile(r"[?#]")
PARTS = re.compile(r"([?&;#])")
# A longer text holding an address is hidden whole rather than checked.
MAX_TEXT = 256 * 1024
TOO_LONG = f"{HIDDEN} (hidden: too long to check for credentials)"
# A text holding more addresses than this is hidden whole too: each costs a
# few microseconds, and anyone who can write an event writes the text.
MAX_ADDRESSES = 2048
TOO_MANY = f"{HIDDEN} (hidden: too many addresses to check for credentials)"
# JSON whose credential cannot be masked in part (a key given twice, or a
# key holding one) is hidden whole.
JSON_HIDDEN = f"{HIDDEN} (hidden: JSON holding a credential that cannot be masked in part)"
# A word with an address starting inside it, hidden whole.
MID_WORD = f"{HIDDEN}://{HIDDEN}"
PREFIX_MARK = re.compile(r"[@:/\\]")
# The k3s and RKE2 Helm controller: its Job's pods carry the chart's label,
# and the Jobs are named for what they do. Rancher runs helm in
# cattle-system's helm-operation-* pods.
CHART_LABEL = "helmcharts.helm.cattle.io/chart"
HELM_JOBS = ("helm-install-", "helm-delete-")
RANCHER = ("cattle-system", "helm-operation-")


def reveal_role():
    import homestead_routes as ROUTER
    return ROUTER.role(REVEAL[1], REVEAL[0]) or "admin"


def sees_values(role):
    import homestead_auth as AUTH
    return AUTH.allows(role, reveal_role())


def _generated(word):
    """A segment or bare parameter that looks like a secret: a credential
    word, a bot token, or a long generated-looking string."""
    if _secret_word(word) >= 0 or BOT.match(word):
        return True
    if len(word) < LONG:
        return False
    return bool((LETTER.search(word) and DIGIT.search(word)) or HEX.fullmatch(word)
                or (BASE64.fullmatch(word) and UPPER.search(word) and LOWER.search(word)))


def _query(text):
    """The query and fragment ("?..." or "#..."): a parameter keeps its
    value only for a name in SHOWN_PARAMS (and a plain value), every other
    value is name=•••; from a name that is not plain or a bare word that
    looks like a secret, the rest is hidden."""
    if not text:
        return text
    out = []
    for index, piece in enumerate(PARTS.split(text)):
        if index % 2 or not piece:
            out.append(piece)
            continue
        name, equals, value = piece.partition("=")
        if not NAME.fullmatch(name) or (not equals and name.lower() not in SHOWN_PARAMS and _generated(name)):
            return "".join(out) + HIDDEN
        shown = not equals or (name.lower() in SHOWN_PARAMS and VALUE.fullmatch(value))
        out.append(piece if shown else f"{name}={HIDDEN}")
    return "".join(out)


def _path_end(path, host):
    """Where the hidden rest of a path starts (the "/" before the first
    segment that is not plain or holds a secret), or None. Searches the whole
    path at once, so a path of many segments costs no more than one."""
    if len(path) <= 1:
        return None
    found = [hit.start() for hit in (PATH_ODD.search(path), BOT_SEGMENT.search(path)) if hit]
    word = _secret_word(path)
    if word >= 0:
        found.append(word)
    hook = AFTER_HOOK.search(path)
    if hook:
        found.append(hook.end() + 1)
    for long in LONG_SEGMENT.finditer(path):
        if _generated(long.group(0)):
            found.append(long.start())
            break
    if len(path) > 1 and (host.lower() in TOPIC_HOSTS or host.partition(".")[0].lower() in NOTIFY_LABELS) \
            and path not in OWN_MASKS:
        found.append(1)
    return path.rfind("/", 0, min(found) + 1) if found else None


def _mask_token(token):
    """token without its credentials (see the module's notes); a token with
    none is returned as it is."""
    head = HEAD.match(token)
    shown = head.group(1) + "://"
    body = token[head.end():]
    if head.group(1).lower() not in PLAIN_SCHEMES:
        return shown + HIDDEN if body else token
    # Everything before the last "@" is a login, wherever a parser would end
    # the authority ("u:1#x@host", "a:1?@host" and "?next=u:p@host" alike).
    at = body.rfind("@")
    login = HIDDEN + "@" if at >= 0 else ""
    body = body[at + 1:]
    end = HOST_END.search(body)
    host, rest = (body[:end.start()], body[end.start():]) if end else (body, "")
    if not HOST.fullmatch(host) or rest[:1] == "\\":       # some parsers read on past a backslash
        return shown + HIDDEN
    out = shown + login + host
    query = QUERY.search(rest)
    path, rest = (rest[:query.start()], rest[query.start():]) if query else (rest, "")
    cut_at = _path_end(path, host)
    if cut_at is not None:
        return out + path[:cut_at] + "/" + HIDDEN
    out += path + _query(rest)
    return out if login or HIDDEN in out else token


def tokens(text):
    """(start, end) of every URL token, as TOKEN.finditer finds them, in time
    linear in the text: only a ":/" or ":\\" can start one, and its scheme is
    at most 32 characters before it."""
    end = 0
    for colon in COLON.finditer(text):
        at = colon.start()
        if at < end:
            continue
        low = max(end, at - 32)
        first = at
        while first > low and text[first - 1] in SCHEME:
            first -= 1
        while first < at and text[first] not in SCHEME_START:
            first += 1
        if first == at:
            continue
        space = SPACE_AT.search(text, at)
        end = space.start() if space else len(text)
        yield first, end


def _bare_login(word):
    """word without a scheme-less login: "//•••@host", "•••@tcp(...)". The
    login ends at the last "@tcp(" (a password may hold one) or, after
    "//", at the last "@": whichever is later. Everything before it is
    hidden too; only a "//" that starts the word or follows a separator
    ("--repo=//•••@host", the mid-word rule's separators) is kept, and
    never in a DSN."""
    dsn = -1
    for found in DSN.finditer(word):
        dsn = found.start()
    at = word.rfind("@")
    slashes = word.find("//", 0, at) if at > 0 else -1
    if slashes < 0:
        return HIDDEN + word[dsn:] if dsn >= 0 else word
    login = max(at, dsn)
    prefix = word[:slashes]
    if dsn < 0 and not PREFIX_MARK.search(prefix) and (not prefix or prefix[-1] not in SCHEME):
        return word[:slashes + 2] + HIDDEN + word[login:]
    return HIDDEN + word[login:]


def _bare(text):
    return WORD.sub(lambda found: _bare_login(found.group(0)), text) if "@" in text else text


def _strip_word(word, start=None):
    """One whitespace-delimited word. An address that starts inside the word
    (after "@", ":", "/" or "\\", or after scheme characters) is not one a
    parser reads on its own: the whole word is hidden (MID_WORD), since
    what comes before it may be a login. After a separator ("--repo=",
    a quote, a bracket) the address is masked as usual."""
    if start is None:
        start = next((first for first, _ in tokens(word)), None)
    if start is not None:
        prefix = word[:start]
        if start and (PREFIX_MARK.search(prefix) or prefix[-1] in SCHEME):
            return MID_WORD
        return prefix + _mask_token(word[start:])
    return _bare_login(word)


def _word_start(text, low, at):
    """Where the word holding text[at] starts, not before low."""
    first = at
    while first > low and at - first < 64:          # most prefixes are short ("--repo=", a quote)
        if text[first - 1] in SPACE:
            return first
        first -= 1
    return max(low, max(text.rfind(space, low, first) for space in SPACE) + 1)


def _strip_text(text):
    out, done = [], 0
    for start, end in tokens(text):
        word = _word_start(text, done, start)
        out += [_bare(text[done:word]), _strip_word(text[word:end], start - word)]
        done = end
    return "".join(out) + _bare(text[done:])


class _Pairs(list):
    """A JSON object as its (key, value) pairs, a key given twice kept."""


def _plain(node):
    if isinstance(node, _Pairs):
        return {key: _plain(value) for key, value in node}
    if isinstance(node, list):
        return [_plain(value) for value in node]
    return node


def _strip_json(text):
    """A JSON object or list with every decoded string stripped and written
    back compact, or text itself when nothing changed; JSON_HIDDEN when a
    key holds a credential or a key given twice meets one; None when text
    is not one."""
    try:
        data = json.loads(text, object_pairs_hook=_Pairs)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, list):
        return None
    changed = twice = False
    todo = [data]
    while todo:
        node = todo.pop()
        if isinstance(node, _Pairs):
            keys = [key for key, _ in node]
            twice = twice or len(set(keys)) != len(keys)
            for key in keys:
                if strip_url_secrets(key) != key:
                    return JSON_HIDDEN
        for index, item in enumerate(node):
            key, item = item if isinstance(node, _Pairs) else (None, item)
            if isinstance(item, str):
                stripped = strip_url_secrets(item)
                if stripped != item:
                    node[index] = (key, stripped) if isinstance(node, _Pairs) else stripped
                    changed = True
            elif isinstance(item, list):
                todo.append(item)
    if not changed:
        return text
    if twice:
        return JSON_HIDDEN
    try:
        return json.dumps(_plain(data), separators=(",", ":"), ensure_ascii=False)
    except RecursionError:
        return JSON_HIDDEN


def strip_url_secrets(text):
    """text with the credentials of every URL token masked (see the
    module's notes); a text over MAX_TEXT holding one is hidden whole.
    Stripping twice changes nothing."""
    if not isinstance(text, str):
        return text
    address = ":/" in text or ":\\" in text or ("@" in text and ("//" in text or DSN.search(text) is not None))
    if len(text) > MAX_TEXT:
        return TOO_LONG if address or "\\u" in text else text      # "\\u002f": an escaped slash
    if address and text.count(":/") + text.count(":\\") + text.count("\\u00") + text.count("@") > MAX_ADDRESSES:
        return TOO_MANY
    # JSON first: an escaped address ("https:\\/\\/", "\\u002f") shows
    # only once decoded.
    if text.lstrip(SPACE)[:1] in ("{", "["):
        stripped = _strip_json(text)
        if stripped is not None:
            return stripped
    return _strip_text(text) if address else text


def cut(text, length, mark="…"):
    """text shortened to length characters, never inside a URL token: one
    that would be cut goes whole. Tokens end only at ASCII whitespace, so a
    cut text holds whole tokens of the full text, and masking it gives the
    same as masking first and cutting after."""
    text = str(text or "")
    if len(text) <= length:
        return text
    for start, end in tokens(text):
        if start >= length:
            break
        if end > length:
            return text[:_word_start(text, 0, start)] + mark      # with its word: a prefix may be a login
    # Nor inside a word that stripping would change (a login without a
    # scheme, an address starting inside the word).
    if text[length - 1:length].strip(SPACE) and text[length:length + 1].strip(SPACE):
        space = SPACE_AT.search(text, length)
        start = _word_start(text, 0, length)
        word = text[start:space.start() if space else len(text)]
        if _strip_word(word) != word:
            return text[:start] + mark
    return text[:length]


def squeeze(text):
    """Runs of ASCII whitespace as one space. Only ASCII whitespace ends a
    URL token, so this never joins or splits one: masking before or after
    gives the same."""
    return SPACES.sub(" ", str(text or "")).strip(SPACE)


def strip_in_place(node, at=""):
    """Strips every text in a map or list, in place; returns the paths changed."""
    changed = []
    pairs = list(node.items()) if isinstance(node, dict) else list(enumerate(node)) if isinstance(node, list) else []
    for key, item in pairs:
        path = f"{at}[{key}]" if isinstance(node, list) else f"{at}.{key}" if at else str(key)
        if isinstance(item, str):
            stripped = strip_url_secrets(item)
            if stripped != item:
                node[key] = stripped
                changed.append(path)
        else:
            changed += strip_in_place(item, path)
    return changed


def has_stripped(value):
    """Whether a value holds what strip_url_secrets puts in (an address
    holding "•••", or the too-long note): saved back, it would replace the
    real credential with the mask."""
    if isinstance(value, dict):
        return any(has_stripped(item) for item in value.values())
    if isinstance(value, list):
        return any(has_stripped(item) for item in value)
    return isinstance(value, str) and (TOO_LONG in value or TOO_MANY in value or JSON_HIDDEN in value or HIDDEN + "@" in value or MID_WORD in value or (HIDDEN in value and any(
        HIDDEN in value[start:end] for start, end in tokens(value))))


# Fields that name a list's items (env and containers by name, ConfigMap
# items by key, mounts by path, ports by number): a saved copy's items are
# matched to the stored ones by them, so reordering never moves a value.
LIST_KEYS = ("name", "key", "mountPath", "devicePath", "containerPort", "ip")
REFUSED = ("is hidden in this copy and is not what is stored there: type the whole address in again, "
           "or open the object as an admin (and reveal a Secret) and edit that copy")


def _list_key(given, stored):
    for field in LIST_KEYS:
        named = [[item.get(field) for item in items] if isinstance(items, list) and all(
            isinstance(item, dict) and isinstance(item.get(field), (str, int)) for item in items) else None
            for items in (given, stored)]
        if all(names is not None and len(set(names)) == len(names) for names in named):
            return field
    return None


def _shown(value):
    """value as a role below reveal sees it."""
    value = copy.deepcopy(value)
    if isinstance(value, str):
        return strip_url_secrets(value)
    strip_in_place(value)
    return value


def restore(given, stored, at=""):
    """Puts back, in place, every text of given that holds a mask and is
    exactly what the text at the same place in stored shows below reveal;
    returns the paths put back. List items are matched by a naming field
    (LIST_KEYS) where every item has one; in a list without, an item that
    holds a mask must be its stored item as shown, at the same place. Any other masked text raises ValueError:
    saved, the mask would replace the credential."""
    put = []
    if isinstance(given, dict):
        pairs = [(key, item, stored.get(key) if isinstance(stored, dict) else None) for key, item in given.items()]
    elif isinstance(given, list):
        field = _list_key(given, stored)
        if field:
            by_name = {item[field]: item for item in stored}
            pairs = [(index, item, by_name.get(item[field])) for index, item in enumerate(given)]
        else:
            # By place: an item added after the stored ones holds no mask, and
            # one added before shifts the masks, which then match nothing.
            known = len(stored) if isinstance(stored, list) else 0
            pairs = [(index, item, stored[index] if index < known else None) for index, item in enumerate(given)]
            for index, item, other in pairs:
                if not isinstance(item, str) and has_stripped(item):
                    if other is None or _shown(other) != item:
                        raise ValueError(f"{at}[{index}] {REFUSED}")
                    given[index] = copy.deepcopy(other)
                    put.append(f"{at}[{index}]")
            pairs = [(index, item, other) for index, item, other in pairs if isinstance(item, str)]
    else:
        return put
    for key, item, other in pairs:
        path = f"{at}[{key}]" if isinstance(given, list) else f"{at}.{key}" if at else str(key)
        if isinstance(item, str):
            if not has_stripped(item):
                continue
            if not isinstance(other, str) or strip_url_secrets(other) != item:
                raise ValueError(f"{path} {REFUSED}")
            given[key] = other
            put.append(path)
        else:
            put += restore(item, other, path)
    return put


def helm_job_pod(pod):
    """Whether a pod is one the Helm controller (or Rancher) runs `helm` in."""
    meta = (pod or {}).get("metadata") or {}
    return (CHART_LABEL in (meta.get("labels") or {})
            or (meta.get("namespace") == RANCHER[0] and str(meta.get("name", "")).startswith(RANCHER[1]))
            or any(str(o.get("apiVersion", "")).startswith("helm.cattle.io/")
                   or (o.get("kind") == "Job" and str(o.get("name", "")).startswith(HELM_JOBS))
                   for o in meta.get("ownerReferences") or []))


def for_role(role, answer):
    """answer as a role sees it: unchanged for the role that may reveal a
    Secret; for any other, a copy with every address stripped (the original
    may be a cached answer shared with admins)."""
    if sees_values(role):
        return answer
    answer = copy.deepcopy(answer)
    if isinstance(answer, str):
        return strip_url_secrets(answer)
    strip_in_place(answer)
    return answer


def host(url):
    """Where an address points, as host[:port], never its login: what a page
    shows as an image's source. Empty when it does not parse."""
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        port = parts.port
    except ValueError:
        return ""
    if not parts.hostname:
        return ""
    name = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{name}:{port}" if port else name
