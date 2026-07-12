#include <algorithm>
#include <cstdint>
#include <cstring>
#include <deque>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

constexpr int kMaxDistance = 4095;
constexpr int kMaxMatchLength = 271;
constexpr int kMaxRunLength = 272;
constexpr int kFastGreedyCandidates = 64;
constexpr int kOptimalCandidateSteps[] = {16, 64, 256};

struct Token {
    char kind;
    int length;
    int value;
    int position;
};

int align4(int value) {
    return (value + 3) & ~3;
}

void write_u32(std::vector<uint8_t>& output, size_t offset, uint32_t value) {
    output[offset + 0] = static_cast<uint8_t>(value & 0xff);
    output[offset + 1] = static_cast<uint8_t>((value >> 8) & 0xff);
    output[offset + 2] = static_cast<uint8_t>((value >> 16) & 0xff);
    output[offset + 3] = static_cast<uint8_t>((value >> 24) & 0xff);
}

uint32_t key3(const std::vector<uint8_t>& data, int position) {
    return (static_cast<uint32_t>(data[position]) << 16) |
           (static_cast<uint32_t>(data[position + 1]) << 8) |
           static_cast<uint32_t>(data[position + 2]);
}

int match_length(const std::vector<uint8_t>& data, int position, int distance, int max_length) {
    int length = 0;
    int match_position = position - distance;
    while (length < max_length && data[position + length] == data[match_position + length]) {
        ++length;
    }
    return length;
}

int run_length(const std::vector<uint8_t>& data, int position, int limit = kMaxRunLength) {
    uint8_t value = data[position];
    int end = std::min<int>(data.size(), position + limit);
    int length = 1;
    while (position + length < end && data[position + length] == value) {
        ++length;
    }
    return length;
}

std::pair<int, int> best_match(
    const std::vector<uint8_t>& data,
    const std::unordered_map<uint32_t, std::vector<int>>& positions,
    int position,
    int max_candidates
) {
    int max_distance = std::min(kMaxDistance, position);
    if (max_distance <= 0 || position + 3 > static_cast<int>(data.size())) {
        return {0, 0};
    }

    int max_length = std::min<int>(kMaxMatchLength, data.size() - position);
    int best_length = 0;
    int best_distance = 0;

    for (int distance = 1; distance <= std::min(2, max_distance); ++distance) {
        int length = match_length(data, position, distance, max_length);
        if (length >= 3 && length > best_length) {
            best_length = length;
            best_distance = distance;
            if (best_length == max_length) {
                return {best_length, best_distance};
            }
        }
    }

    auto found = positions.find(key3(data, position));
    if (found == positions.end()) {
        return {best_length, best_distance};
    }

    int start = position - max_distance;
    int checked = 0;
    const auto& candidates = found->second;
    for (auto iter = candidates.rbegin(); iter != candidates.rend(); ++iter) {
        int candidate = *iter;
        if (candidate < start) {
            break;
        }
        int distance = position - candidate;
        if (distance <= 2) {
            continue;
        }
        ++checked;
        if (best_length >= 3 && best_length < max_length &&
            data[candidate + best_length] != data[position + best_length]) {
            if (max_candidates > 0 && checked >= max_candidates) {
                break;
            }
            continue;
        }
        int length = match_length(data, position, distance, max_length);
        if (length > best_length) {
            best_length = length;
            best_distance = distance;
            if (length == max_length) {
                break;
            }
        }
        if (max_candidates > 0 && checked >= max_candidates) {
            break;
        }
    }

    return {best_length, best_distance};
}

void add_position(
    const std::vector<uint8_t>& data,
    std::unordered_map<uint32_t, std::vector<int>>& positions,
    int position
) {
    if (position + 3 <= static_cast<int>(data.size())) {
        positions[key3(data, position)].push_back(position);
    }
}

void add_range(
    const std::vector<uint8_t>& data,
    std::unordered_map<uint32_t, std::vector<int>>& positions,
    int start,
    int end
) {
    for (int position = start; position < end; ++position) {
        add_position(data, positions, position);
    }
}

bool choose_token(
    const std::vector<uint8_t>& data,
    const std::unordered_map<uint32_t, std::vector<int>>& positions,
    int position,
    int max_candidates,
    Token& token
) {
    int full_run = run_length(data, position);
    int rle_length = full_run >= 3 ? std::min(full_run, 18) : 0;
    auto [match_len, distance] = best_match(data, positions, position, max_candidates);

    if (rle_length >= 3 && (match_len > 0 || full_run <= 36) && rle_length >= match_len) {
        token = {'R', rle_length, data[position], position};
        return true;
    }
    if (match_len >= 3) {
        token = {'M', std::min(match_len, kMaxMatchLength), distance, position};
        return true;
    }
    return false;
}

std::vector<Token> greedy_tokens(const std::vector<uint8_t>& data, int max_candidates) {
    std::unordered_map<uint32_t, std::vector<int>> positions;
    positions.reserve(data.size() / 2);
    std::vector<Token> tokens;
    tokens.reserve(data.size() / 2);

    int position = 0;
    while (position < static_cast<int>(data.size())) {
        Token token{};
        if (choose_token(data, positions, position, max_candidates, token)) {
            tokens.push_back(token);
            int new_position = position + token.length;
            add_range(data, positions, position, new_position);
            position = new_position;
            continue;
        }

        int start = position;
        ++position;
        add_range(data, positions, start, position);
        while (position < static_cast<int>(data.size()) && position - start < 4113) {
            if (choose_token(data, positions, position, max_candidates, token)) {
                break;
            }
            add_range(data, positions, position, position + 1);
            ++position;
        }

        int length = position - start;
        if (length >= 18) {
            tokens.push_back({'B', length, 0, start});
        } else {
            for (int literal_position = start; literal_position < position; ++literal_position) {
                tokens.push_back({'L', 1, data[literal_position], literal_position});
            }
        }
    }

    return tokens;
}

int token_payload_size(const Token& token) {
    if (token.kind == 'L') {
        return 1;
    }
    if (token.kind == 'R') {
        return 2;
    }
    if (token.kind == 'B') {
        return token.length + 2;
    }
    if (token.kind == 'M') {
        return token.length >= 16 ? 3 : 2;
    }
    throw std::runtime_error("unknown token kind");
}

bool encode_tokens(
    const std::vector<uint8_t>& data,
    const std::vector<Token>& tokens,
    int stored_size,
    std::vector<uint8_t>& encoded
) {
    std::vector<uint8_t> output;
    output.reserve(stored_size > 0 ? stored_size : data.size() + 16);
    output.push_back('S');
    output.push_back('D');
    output.push_back('0');
    output.push_back('\0');
    output.resize(12, 0);
    write_u32(output, 8, static_cast<uint32_t>(data.size()));

    std::vector<Token> group;
    group.reserve(8);
    auto flush_group = [&]() {
        if (group.empty()) {
            return;
        }
        uint8_t flags = 0;
        std::vector<uint8_t> payload;
        for (size_t bit = 0; bit < group.size(); ++bit) {
            const Token& token = group[bit];
            if (token.kind == 'L') {
                payload.push_back(static_cast<uint8_t>(token.value));
                continue;
            }

            flags |= static_cast<uint8_t>(1u << bit);
            if (token.kind == 'R') {
                payload.push_back(static_cast<uint8_t>(((token.length - 3) << 4) | 1));
                payload.push_back(static_cast<uint8_t>(token.value));
            } else if (token.kind == 'B') {
                int encoded_length = token.length - 18;
                payload.push_back(static_cast<uint8_t>(((encoded_length & 0x0f) << 4) | 2));
                payload.push_back(static_cast<uint8_t>(encoded_length >> 4));
                payload.insert(
                    payload.end(),
                    data.begin() + token.position,
                    data.begin() + token.position + token.length
                );
            } else if (token.kind == 'M') {
                int distance = token.value;
                if (token.length >= 16) {
                    payload.push_back(static_cast<uint8_t>((distance & 0x0f) << 4));
                    payload.push_back(static_cast<uint8_t>(distance >> 4));
                    payload.push_back(static_cast<uint8_t>(token.length - 16));
                } else {
                    payload.push_back(static_cast<uint8_t>(((distance & 0x0f) << 4) | token.length));
                    payload.push_back(static_cast<uint8_t>(distance >> 4));
                }
            } else {
                throw std::runtime_error("unknown token kind");
            }
        }
        output.push_back(flags);
        output.insert(output.end(), payload.begin(), payload.end());
        group.clear();
    };

    for (const Token& token : tokens) {
        group.push_back(token);
        if (group.size() == 8) {
            flush_group();
        }
    }
    flush_group();

    int actual_size = align4(static_cast<int>(output.size()));
    int final_size = stored_size > 0 ? stored_size : actual_size;
    if (actual_size > final_size || (final_size & 3) != 0) {
        return false;
    }
    write_u32(output, 4, static_cast<uint32_t>(final_size));
    output.resize(final_size, 0);
    encoded = std::move(output);
    return true;
}

std::vector<Token> collapse_literal_tokens(const std::vector<Token>& tokens) {
    std::vector<Token> collapsed;
    collapsed.reserve(tokens.size());
    size_t index = 0;
    while (index < tokens.size()) {
        const Token& first = tokens[index];
        if (first.kind != 'L') {
            collapsed.push_back(first);
            ++index;
            continue;
        }

        size_t start = index;
        int position = first.position;
        while (
            index < tokens.size() &&
            tokens[index].kind == 'L' &&
            tokens[index].position == position + static_cast<int>(index - start)
        ) {
            ++index;
        }

        int literal_count = static_cast<int>(index - start);
        int offset = 0;
        while (literal_count - offset >= 18) {
            int remaining = literal_count - offset;
            int chunk_length = std::min(4113, remaining);
            if (remaining - chunk_length != 0 && remaining - chunk_length < 18) {
                chunk_length -= 18;
            }
            collapsed.push_back({'B', chunk_length, 0, position + offset});
            offset += chunk_length;
        }
        while (offset < literal_count) {
            const Token& literal = tokens[start + offset];
            collapsed.push_back({'L', 1, literal.value, position + offset});
            ++offset;
        }
    }
    return collapsed;
}

std::vector<Token> optimal_tokens(const std::vector<uint8_t>& data, int max_candidates) {
    int n = static_cast<int>(data.size());
    std::vector<std::pair<uint16_t, uint16_t>> matches(n);
    std::vector<uint16_t> runs(n);
    std::unordered_map<uint32_t, std::vector<int>> positions;
    positions.reserve(data.size() / 2);

    for (int position = 0; position < n; ++position) {
        auto [length, distance] = best_match(data, positions, position, max_candidates);
        matches[position] = {
            static_cast<uint16_t>(length),
            static_cast<uint16_t>(distance)
        };
        runs[position] = static_cast<uint16_t>(run_length(data, position));
        add_position(data, positions, position);
    }

    std::vector<int> costs((n + 1) * 8, 0);
    std::vector<uint8_t> choice_kind(n * 8, 0);
    std::vector<uint16_t> choice_length(n * 8, 0);
    std::vector<uint16_t> choice_value(n * 8, 0);
    std::vector<std::deque<std::pair<int, int>>> literal_block_windows(8);

    for (int position = n - 1; position >= 0; --position) {
        int literal_block_end = position + 18;
        if (literal_block_end <= n) {
            for (int token_mod = 0; token_mod < 8; ++token_mod) {
                int value = literal_block_end + costs[literal_block_end * 8 + token_mod];
                auto& window = literal_block_windows[token_mod];
                while (!window.empty() && window.back().second >= value) {
                    window.pop_back();
                }
                window.push_back({literal_block_end, value});
            }
        }
        int literal_block_limit = position + 4113;
        for (auto& window : literal_block_windows) {
            while (!window.empty() && window.front().first > literal_block_limit) {
                window.pop_front();
            }
        }

        int state_base = position * 8;
        int byte_value = data[position];
        int rle_limit = std::min<int>(runs[position], 18);
        int match_length_value = matches[position].first;
        int match_distance = matches[position].second;
        int match_limit = std::min(match_length_value, kMaxMatchLength);

        for (int token_mod = 0; token_mod < 8; ++token_mod) {
            int next_mod = (token_mod + 1) & 7;
            int flag_cost = token_mod == 0 ? 1 : 0;
            int best_cost = flag_cost + 1 + costs[(position + 1) * 8 + next_mod];
            int best_kind = 0;
            int best_length = 1;
            int best_value = byte_value;

            if (rle_limit >= 3) {
                for (int length = 3; length <= rle_limit; ++length) {
                    int cost = flag_cost + 2 + costs[(position + length) * 8 + next_mod];
                    if (cost < best_cost) {
                        best_cost = cost;
                        best_kind = 1;
                        best_length = length;
                        best_value = byte_value;
                    }
                }
            }

            if (match_limit >= 3) {
                for (int length = 3; length <= match_limit; ++length) {
                    int payload_cost = length >= 16 ? 3 : 2;
                    int cost = flag_cost + payload_cost + costs[(position + length) * 8 + next_mod];
                    if (cost < best_cost) {
                        best_cost = cost;
                        best_kind = 2;
                        best_length = length;
                        best_value = match_distance;
                    }
                }
            }

            const auto& literal_block_window = literal_block_windows[next_mod];
            if (!literal_block_window.empty()) {
                int literal_end = literal_block_window.front().first;
                int literal_length = literal_end - position;
                int cost = flag_cost + literal_length + 2 + costs[literal_end * 8 + next_mod];
                if (cost < best_cost) {
                    best_cost = cost;
                    best_kind = 3;
                    best_length = literal_length;
                    best_value = 0;
                }
            }

            int state = state_base + token_mod;
            costs[state] = best_cost;
            choice_kind[state] = static_cast<uint8_t>(best_kind);
            choice_length[state] = static_cast<uint16_t>(best_length);
            choice_value[state] = static_cast<uint16_t>(best_value);
        }
    }

    std::vector<Token> tokens;
    tokens.reserve(n / 2);
    int position = 0;
    int token_mod = 0;
    while (position < n) {
        int state = position * 8 + token_mod;
        uint8_t kind_code = choice_kind[state];
        int length = choice_length[state];
        int value = choice_value[state];
        if (kind_code == 0) {
            tokens.push_back({'L', 1, value, position});
        } else if (kind_code == 1) {
            tokens.push_back({'R', length, value, position});
        } else if (kind_code == 2) {
            tokens.push_back({'M', length, value, position});
        } else if (kind_code == 3) {
            tokens.push_back({'B', length, 0, position});
        } else {
            throw std::runtime_error("unknown SD0 token choice");
        }
        position += tokens.back().length;
        token_mod = (token_mod + 1) & 7;
    }

    return tokens;
}

std::vector<uint8_t> read_stdin() {
    std::vector<uint8_t> data;
    constexpr size_t kChunk = 1 << 16;
    std::vector<char> buffer(kChunk);
    while (std::cin.good()) {
        std::cin.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        std::streamsize count = std::cin.gcount();
        if (count > 0) {
            data.insert(data.end(), buffer.begin(), buffer.begin() + count);
        }
    }
    return data;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        int stored_size = 0;
        for (int index = 1; index < argc; ++index) {
            std::string arg = argv[index];
            if (arg == "--stored-size" && index + 1 < argc) {
                stored_size = std::stoi(argv[++index]);
            } else {
                std::cerr << "unknown argument: " << arg << "\n";
                return 2;
            }
        }
        if (stored_size <= 0) {
            std::cerr << "--stored-size is required\n";
            return 2;
        }

        std::vector<uint8_t> data = read_stdin();
        if (data.size() >= 0x1000000u) {
            std::cerr << "SD0 header only supports unpacked sizes below 16 MiB\n";
            return 2;
        }

        std::vector<uint8_t> encoded;
        auto greedy = greedy_tokens(data, kFastGreedyCandidates);
        if (encode_tokens(data, greedy, stored_size, encoded)) {
            std::cout.write(reinterpret_cast<const char*>(encoded.data()), encoded.size());
            return 0;
        }

        for (int cap : kOptimalCandidateSteps) {
            auto optimal = optimal_tokens(data, cap);
            if (encode_tokens(data, optimal, stored_size, encoded)) {
                std::cout.write(reinterpret_cast<const char*>(encoded.data()), encoded.size());
                return 0;
            }
        }

        std::vector<uint8_t> unbounded;
        encode_tokens(data, greedy, 0, unbounded);
        std::cerr << "compressed SD0 record is " << unbounded.size()
                  << " bytes, larger than original " << stored_size
                  << " byte slot; fast native fallback also failed\n";
        return 1;
    } catch (const std::exception& exc) {
        std::cerr << exc.what() << "\n";
        return 2;
    }
}
