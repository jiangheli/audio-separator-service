from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    output = Path(__file__).resolve().parent / "bundle" / "stemflow.ico"
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", (256, 256), "#7657e8")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((0, 0, 255, 255), radius=56, fill="#7657e8")
    star = [
        (128, 38),
        (145, 102),
        (210, 128),
        (145, 151),
        (128, 218),
        (108, 151),
        (45, 128),
        (108, 102),
    ]
    draw.polygon(star, fill="white")
    draw.ellipse((184, 180, 220, 216), fill="#c9bbff")
    canvas.save(
        output,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
