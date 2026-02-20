LEVEL_MULTIPLIERS = {
    1: 1.0,
    2: 1.2,
    3: 1.5,
    4: 2.0,
    5: 3.0,
}


def calculate_power(attack: int, defense: int, speed: int, level: int = 1) -> int:
    """计算角色战力：(攻击 * 2 + 防御 * 1.5 + 速度) * 等级倍率"""
    if level not in LEVEL_MULTIPLIERS:
        raise ValueError(f"等级必须在 1~5 之间，当前：{level}")
    base = attack * 2 + defense * 1.5 + speed
    return int(base * LEVEL_MULTIPLIERS[level])


if __name__ == "__main__":
    examples = [
        ("初心者", 10, 8, 12, 1),
        ("中级战士", 30, 25, 20, 3),
        ("传说英雄", 80, 60, 70, 5),
    ]
    print(f"{'角色':<10} {'战力':>8}")
    print("-" * 20)
    for name, atk, df, spd, lv in examples:
        power = calculate_power(atk, df, spd, lv)
        print(f"{name:<10} {power:>8}")
