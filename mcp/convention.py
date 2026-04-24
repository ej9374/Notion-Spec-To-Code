def get_convention_preset() -> list[str]:
    return [
        "@Builder",
        "@Getter",
        "@NoArgsConstructor(access = AccessLevel.PROTECTED)",
    ]
