import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from bump_manager import BumpManager


def _write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def main():
    tmp_dir = os.path.join(os.path.dirname(__file__), '_tmp_bumps')
    os.makedirs(tmp_dir, exist_ok=True)

    script_path = os.path.join(tmp_dir, 'syntax_test.txt')
    _write(
        script_path,
        """
<bump music=any>
<card>
Standard timing card.
<card 500ms>
Absolute 500ms card.
<card +500ms>
Plus 500ms card.
<card -500ms>
Minus 500ms card.
<card 5s>
Five seconds card.
<card>
Whitespace tags: A<\\s>B<\\t>C<\\n>D
<card>
Sound FX test: hello there <sound ding.wav interrupt 500ms>
<card>
Sound CUT test: hello there <sound ding.wav cut card>
<card>
Before image line.
<img test.png 20%>
After image line.
<card>
<img test2.png lines>
Image tag on first line (used to get dropped).
<outro="[sleepy shows]" audio>
""".lstrip(),
    )

    bm = BumpManager()
    bm.load_bumps(tmp_dir)
    assert bm.bump_scripts, 'No scripts parsed'
    script = bm.bump_scripts[0]

    cards = script.get('cards', [])
    assert len(cards) == 11, f'Expected 11 cards, got {len(cards)}'

    std = cards[0]['duration']
    abs_500 = cards[1]['duration']
    plus = cards[2]['duration']
    minus = cards[3]['duration']
    five_s = cards[4]['duration']
    ws_card = cards[5]
    
    sound_card = cards[6]
    cut_card = cards[7]
    img_card = cards[8]
    img_firstline_card = cards[9]
    outro_card = cards[10]
    assert img_card.get('type') in ('img', 'text'), f"Expected img card or fallback text, got {img_card.get('type')}"
    assert img_firstline_card.get('type') in ('img', 'text'), f"Expected img card or fallback text, got {img_firstline_card.get('type')}"

    print('Standard duration:', std)
    print('Abs 500 duration:', abs_500)
    print('Plus duration:', plus)
    print('Minus duration:', minus)
    print('5s duration:', five_s)

    assert abs_500 == 500 or abs_500 == bm._clamp_card_duration_ms(500)
    assert plus >= std, 'Expected +500ms to be >= standard'
    assert minus <= std, 'Expected -500ms to be <= standard'
    assert five_s == 5000 or five_s == bm._clamp_card_duration_ms(5000)

    # Whitespace tags should expand.
    assert ws_card.get('type') == 'text'
    ws_text = ws_card.get('text', '')
    assert 'A B' in ws_text
    assert '\t' in ws_text
    assert '\n' in ws_text

    # Sound tag should be parsed and stripped from text.
    assert sound_card.get('type') == 'text'
    assert '<sound' not in sound_card.get('text', '').lower()
    sfx = sound_card.get('sound')
    assert isinstance(sfx, dict), 'Expected sound info on card'
    assert sfx.get('filename') == 'ding.wav'
    assert sfx.get('mix') == 'interrupt'
    assert sfx.get('play_for') == 'ms'
    assert int(sfx.get('ms', 0)) == 500

    # Cut mode should be recognized.
    assert cut_card.get('type') == 'text'
    assert '<sound' not in cut_card.get('text', '').lower()
    sfx2 = cut_card.get('sound')
    assert isinstance(sfx2, dict), 'Expected sound info on cut card'
    assert sfx2.get('filename') == 'ding.wav'
    assert sfx2.get('mix') == 'cut'
    assert sfx2.get('play_for') == 'card'

    # Outro audio flag should parse.
    assert outro_card.get('type') == 'text'
    assert outro_card.get('duration') == 800
    assert outro_card.get('outro_audio') is True

    print('OK')


if __name__ == '__main__':
    main()
