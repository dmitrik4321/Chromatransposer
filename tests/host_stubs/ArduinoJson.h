// ============================================================================
// Хостовая заглушка ArduinoJson v7 для smoke-тестов компиляции ct_ir.h.
// НЕ часть прошивки: только тестовая сборка (g++ -DCT_HOST_TEST).
// Покрывает ровно тот API, который трогает ct_ir.h: JsonDocument, Variant-
// цепочки operator[], as<T>/as<T>(def), is<const char*>(), range-for по
// JsonArray, deserializeJson. Парсер — минимальный рекурсивный descent.
// Если в ct_ir.h появится новый приём ArduinoJson — дописываем сюда же.
// ============================================================================
#ifndef CT_STUB_ARDUINOJSON_H
#define CT_STUB_ARDUINOJSON_H

#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace ctjson {

struct Node;
using NodePtr = std::shared_ptr<Node>;

struct Node {
    enum Kind { Null, Bool, Num, Str, Arr, Obj } kind = Null;
    bool b = false;
    double num = 0;
    std::string str;
    std::vector<NodePtr> arr;
    std::map<std::string, NodePtr> obj;
};

inline void skipws(const char*& p) {
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
}

inline NodePtr parseValue(const char*& p);

inline NodePtr parseString(const char*& p) {
    auto n = std::make_shared<Node>();
    n->kind = Node::Str;
    ++p;  // открывающая кавычка
    while (*p && *p != '"') {
        if (*p == '\\' && p[1]) ++p;
        n->str += *p++;
    }
    if (*p == '"') ++p;
    return n;
}

inline NodePtr parseObject(const char*& p) {
    auto n = std::make_shared<Node>();
    n->kind = Node::Obj;
    ++p;  // '{'
    skipws(p);
    if (*p == '}') { ++p; return n; }
    while (*p) {
        skipws(p);
        if (*p != '"') return nullptr;      // строгий разбор: ждём ключ
        auto key = parseString(p);
        skipws(p);
        if (*p != ':') return nullptr;
        ++p;
        auto val = parseValue(p);
        if (!val) return nullptr;
        n->obj[key->str] = val;
        skipws(p);
        if (*p == ',') { ++p; continue; }
        if (*p == '}') { ++p; return n; }
        return nullptr;                    // мусор после значения
    }
    return nullptr;                        // обрыв внутри объекта
}

inline NodePtr parseArray(const char*& p) {
    auto n = std::make_shared<Node>();
    n->kind = Node::Arr;
    ++p;  // '['
    skipws(p);
    if (*p == ']') { ++p; return n; }
    while (*p) {
        auto el = parseValue(p);
        if (!el) return nullptr;
        n->arr.push_back(el);
        skipws(p);
        if (*p == ',') { ++p; continue; }
        if (*p == ']') { ++p; return n; }
        return nullptr;
    }
    return nullptr;  // обрыв внутри массива
}

inline NodePtr parseValue(const char*& p) {
    skipws(p);
    auto n = std::make_shared<Node>();
    if (!*p) return n;
    if (*p == '{') return parseObject(p);
    if (*p == '[') return parseArray(p);
    if (*p == '"') return parseString(p);
    if (!strncmp(p, "true", 4))  { n->kind = Node::Bool; n->b = true;  p += 4; return n; }
    if (!strncmp(p, "false", 5)) { n->kind = Node::Bool; n->b = false; p += 5; return n; }
    if (!strncmp(p, "null", 4))  { p += 4; return n; }
    char* end = nullptr;
    n->num = strtod(p, &end);
    if (end == p) return nullptr;  // мусор -> ошибка разбора
    n->kind = Node::Num;
    p = end;
    return n;
}

// Строгий разбор: после значения допустимы только пробельные символы и конец.
inline NodePtr parseStrict(const char* json, size_t len) {
    std::string s(json, len);
    const char* p = s.c_str();
    auto root = parseValue(p);
    if (!root) return nullptr;
    skipws(p);
    if (*p) return nullptr;   // хвост-мусор => битый JSON (как ArduinoJson)
    return root;
}

}  // namespace ctjson

// ---- публичный API ----------------------------------------------------------

class Variant;
class JsonObject;
class JsonArray;
using VariantHandle    = Variant;
using JsonObjectHandle = JsonObject;
using JsonArrayHandle  = JsonArray;

class Variant {
public:
    ctjson::NodePtr n;
    Variant() = default;
    explicit Variant(ctjson::NodePtr p) : n(std::move(p)) {}

    bool isNull() const { return !n || n->kind == ctjson::Node::Null; }

    operator const char*() const;   // определение после специализаций as<>
    template <typename T> T as() const;
    template <typename T> T as(T def) const {
        if (isNull()) return def;
        return as<T>();
    }

    template <typename T> bool is() const;

    Variant operator[](const char* k) const {
        if (!n || n->kind != ctjson::Node::Obj) return Variant();
        auto it = n->obj.find(k);
        return it == n->obj.end() ? Variant() : Variant(it->second);
    }
    Variant operator[](size_t i) const {
        if (!n || n->kind != ctjson::Node::Arr || i >= n->arr.size()) return Variant();
        return Variant(n->arr[i]);
    }
    size_t size() const { return (n && n->kind == ctjson::Node::Arr) ? n->arr.size() : 0; }

    // range-for: for (VariantHandle v : arr)
    class It {
    public:
        const std::vector<ctjson::NodePtr>* a;
        size_t i;
        bool operator!=(const It& o) const { return i != o.i; }
        It& operator++() { ++i; return *this; }
        Variant operator*() const { return Variant((*a)[i]); }
    };
    static const std::vector<ctjson::NodePtr>& empty_vec() {
        static const std::vector<ctjson::NodePtr> e;
        return e;
    }
    It begin() const { return It{n ? &n->arr : &empty_vec(), 0}; }
    It end() const { return It{n ? &n->arr : &empty_vec(), n ? n->arr.size() : 0}; }
};

template <> inline bool      Variant::as<bool>() const {
    if (!n) return false;
    if (n->kind == ctjson::Node::Bool) return n->b;
    if (n->kind == ctjson::Node::Num)  return n->num != 0;
    return false;
}
template <> inline long      Variant::as<long>() const {
    return (n && n->kind == ctjson::Node::Num) ? (long)n->num : 0L;
}
template <> inline int       Variant::as<int>() const { return (int)as<long>(); }
template <> inline uint32_t  Variant::as<uint32_t>() const { return (uint32_t)as<long>(); }
template <> inline uint16_t  Variant::as<uint16_t>() const { return (uint16_t)as<long>(); }
template <> inline float     Variant::as<float>() const {
    return (n && n->kind == ctjson::Node::Num) ? (float)n->num : 0.f;
}
template <> inline const char* Variant::as<const char*>() const {
    return (n && n->kind == ctjson::Node::Str) ? n->str.c_str() : nullptr;
}
// .as<JsonObject>() / .as<JsonArray>() — маркерные конверсии как в v7.
template <> inline JsonObject Variant::as<JsonObject>() const;
template <> inline JsonArray  Variant::as<JsonArray>() const;

// Обёртки: convertible to bool (для `if (JsonObjectHandle h = ...)`),
// и индексация через внутренний Variant.
class JsonObject {
public:
    Variant v;
    JsonObject() = default;
    JsonObject(Variant x) : v(x) {}   // неявная конверсия как в v7
    explicit operator bool() const {
        return !v.isNull() && v.n && v.n->kind == ctjson::Node::Obj;
    }
    Variant operator[](const char* k) const { return v[k]; }
};

class JsonArray {
public:
    Variant v;
    JsonArray() = default;
    JsonArray(Variant x) : v(x) {}    // неявная конверсия как в v7
    explicit operator bool() const { return !v.isNull(); }
    size_t size() const { return v.size(); }
    Variant operator[](size_t i) const { return v[i]; }
    // В ArduinoJson v7 JsonArrayHandle — это Variant, и код ct_ir.h индексирует
    // "params" (объект) строковыми ключами через псевдоним JsonArrayHandle.
    // Разрешаем ту же индексацию здесь, чтобы фикстура не ломала прошивочный код.
    Variant operator[](const char* k) const { return v[k]; }
    Variant::It begin() const { return v.begin(); }
    Variant::It end() const { return v.end(); }
};

template <> inline JsonObject Variant::as<JsonObject>() const { return JsonObject(*this); }
template <> inline JsonArray  Variant::as<JsonArray>() const { return JsonArray(*this); }

// Единственная неявная конверсия, которая нужна ct_ir.h: Variant -> const char*
// (ctParseClass(doc["class"])). Объявлена после специализаций as<> — иначе GCC
// запрещает «specialization after instantiation».
inline Variant::operator const char*() const { return as<const char*>(); }

// is<T>() — честные проверки типа узла.
template <> inline bool Variant::is<const char*>() const { return n && n->kind == ctjson::Node::Str; }
template <> inline bool Variant::is<bool>() const { return n && n->kind == ctjson::Node::Bool; }
template <> inline bool Variant::is<float>() const { return n && n->kind == ctjson::Node::Num; }
template <> inline bool Variant::is<JsonObject>() const { return n && n->kind == ctjson::Node::Obj; }
template <> inline bool Variant::is<JsonArray>() const { return n && n->kind == ctjson::Node::Arr; }

class JsonDocument {
public:
    ctjson::NodePtr root;
    Variant operator[](const char* k) const {
        if (!root || root->kind != ctjson::Node::Obj) return Variant();
        auto it = root->obj.find(k);
        return it == root->obj.end() ? Variant() : Variant(it->second);
    }
};

struct DeserializationError {
    bool failed = false;
    operator bool() const { return failed; }  // if (err)
};

inline DeserializationError deserializeJson(JsonDocument& doc, const char* json, size_t len) {
    doc.root = ctjson::parseStrict(json, len);
    DeserializationError e;
    e.failed = (doc.root == nullptr);
    return e;
}

#endif  // CT_STUB_ARDUINOJSON_H
