"""Small, on-demand screen rendering with pixel-based line fitting."""
import io
import math


def text_image(text):
    from PIL import Image, ImageDraw, ImageFont
    canvas = Image.new('RGB', (280, 240), 'white')
    draw, font = ImageDraw.Draw(canvas), ImageFont.load_default(size=18)
    lines = []
    for paragraph in text.split('\n'):
        if not paragraph:
            lines.append('')
        while paragraph:
            end = 1
            while end < len(paragraph) and draw.textlength(paragraph[:end + 1], font=font) <= 260:
                end += 1
            boundary = paragraph.rfind(' ', 0, end + 1) if end < len(paragraph) else -1
            if boundary > 0:
                end = boundary
            lines.append(paragraph[:end])
            paragraph = paragraph[end:].lstrip()
    clipped = len(lines) > 9
    lines = lines[:9]
    if clipped:
        while draw.textlength(lines[-1] + '...', font=font) > 260:
            lines[-1] = lines[-1][:-1]
        lines[-1] += '...'
    for index, line in enumerate(lines):
        draw.text((10, 10 + index * 24), line, fill='black', font=font)
    output = io.BytesIO()
    canvas.save(output, format='PNG')
    return output.getvalue(), clipped


def _canvas(artwork=None):
    from PIL import Image, ImageOps
    if artwork is not None:
        try:
            with Image.open(artwork) as source:
                return ImageOps.fit(source.convert('RGB'), (280, 240))
        except (OSError, ValueError):
            pass
    return Image.new('RGB', (280, 240), '#d9f4f0')


def _lines(draw, text, font, width):
    lines = []
    for paragraph in str(text).split('\n'):
        if not paragraph:
            lines.append('')
        while paragraph:
            end = 1
            while end < len(paragraph) and draw.textlength(paragraph[:end + 1], font=font) <= width:
                end += 1
            boundary = paragraph.rfind(' ', 0, end + 1) if end < len(paragraph) else -1
            if boundary > 0:
                end = boundary
            lines.append(paragraph[:end])
            paragraph = paragraph[end:].lstrip()
    return lines


def card_image(text, *, title='Robbi', artwork=None, kind='card', values=(), phase=None):
    """A readable LCD card; illustration and result each have their own space.

    phase is cosmetic only. Randomness is supplied once by the app and the last
    frame always shows those exact values, never a second random draw.
    """
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
    canvas = _canvas(artwork)
    if kind in {'coin', 'dice'}:
        canvas = Image.blend(canvas.filter(ImageFilter.GaussianBlur(6)),
                             Image.new('RGB', (280, 240), '#fff0ba'), .85)
    draw = ImageDraw.Draw(canvas)
    ink, mint = '#173c46', '#9ce8d7'
    small = ImageFont.load_default(size=14)
    regular = ImageFont.load_default(size=18)
    draw.rounded_rectangle((8, 8, 272, 36), radius=12, fill=ink)
    label = str(title)[:40]
    while draw.textlength(label, font=small) > 244:
        label = label[:-1]
    draw.text((140, 22), label, anchor='mm', font=small, fill='white')
    animated = kind in {'coin', 'dice', 'choose', 'shuffle', 'numbers'}
    top = 139 if animated else 111
    if kind == 'coin':
        squeeze = max(.12, abs(math.cos(phase * math.pi * 2))) if phase is not None else 1
        draw.ellipse((86, 174, 194, 185), fill='#dac38d')
        radius = int(66 * squeeze)
        if artwork is not None and artwork.is_file():
            heads = values and values[0] == 'Heads'
            if phase is not None:
                heads = int(phase * 5) % 2 == 0
            from .manifest import confined
            sprite = confined(artwork.parent, 'heads.png' if heads else 'tails.png')
            if sprite.is_file():
                with Image.open(sprite) as original:
                    if original.width > 1024 or original.height > 1024:
                        raise ValueError('Coin sprite exceeds 1024 pixels')
                    face = ImageOps.contain(original.convert('RGBA'), (132, 132), method=Image.Resampling.LANCZOS)
                mask = face.getchannel('A')
            else:
                source = _canvas(artwork)
                box = (36, 0, 188, 152) if heads else (105, 90, 255, 240)
                face = ImageOps.fit(source.crop(box), (132, 132))
                mask = Image.new('L', (132, 132))
                ImageDraw.Draw(mask).ellipse((0, 0, 131, 131), fill=255)
            width = max(12, int(132*squeeze))
            face, mask = face.resize((width, 132)), mask.resize((width, 132))
            bounce = round(6 * math.sin(phase * math.pi)) if phase is not None else 0
            canvas.paste(face, (140-width//2, 43-bounce), mask)
        else:
            draw.ellipse((140-radius,43,140+radius,175),fill='#ffd56d',outline='#b87525',width=4)
            if phase is None:
                draw.text((140,109),'H' if values and values[0]=='Heads' else 'T',anchor='mm',
                          font=ImageFont.load_default(size=48),fill=ink)
        label = str(values[0]).upper() if phase is None and values else 'FLIPPING...'
        draw.text((140,207),label,anchor='mm',font=ImageFont.load_default(size=30),fill=ink,stroke_width=1)
        output = io.BytesIO()
        canvas.save(output, format='PNG')
        return output.getvalue(), False
    if kind == 'dice':
        shown = list(values)[:5] if phase is None else [1 + int(phase * 7) % 6] * min(len(values), 5)
        size = min(100 if len(shown) == 1 else 78, int(234/max(1,len(shown)))-8)
        left = 140 - (len(shown)*(size+8)-8)/2
        for index,value in enumerate(shown):
            x,y=left+index*(size+8),100-size/2
            draw.rounded_rectangle((x+2,y+5,x+size+2,y+size+5),radius=13,fill='#d3bc8a')
            draw.rounded_rectangle((x,y,x+size,y+size),radius=13,fill='#fffefa',outline=ink,width=2)
            if isinstance(value,int) and 1 <= value <= 6:
                dots={1:[(1,1)],2:[(0,0),(2,2)],3:[(0,0),(1,1),(2,2)],
                      4:[(0,0),(2,0),(0,2),(2,2)],5:[(0,0),(2,0),(1,1),(0,2),(2,2)],
                      6:[(0,0),(2,0),(0,1),(2,1),(0,2),(2,2)]}[value]
                for dx,dy in dots:
                    cx,cy=x+size*(.25+.25*dx),y+size*(.25+.25*dy)
                    r=max(2,size*.06)
                    draw.ellipse((cx-r,cy-r,cx+r,cy+r),fill=ink)
            else:
                draw.text((x+size/2,y+size/2),str(value),anchor='mm',font=ImageFont.load_default(size=30 if size>60 else 18),fill=ink)
        if phase is not None:
            draw.text((140,195),'ROLLING...',anchor='mm',font=ImageFont.load_default(size=28),fill=ink)
            clipped=False
        else:
            total=sum(value for value in values if isinstance(value,int))
            draw.text((140,179),f'TOTAL {total}',anchor='mm',font=ImageFont.load_default(size=30),fill=ink,stroke_width=1)
            if len(values)>5:
                draw.text((140,145),f'{len(values)} dice · first 5 pictured',anchor='mm',font=small,fill=ink)
            caption=_lines(draw,' + '.join(map(str,values)),small,250)
            clipped=len(caption)>2
            for index,line in enumerate(caption[:2]):
                draw.text((140,207+index*16),line,anchor='mm',font=small,fill=ink)
        output=io.BytesIO()
        canvas.save(output,format='PNG')
        return output.getvalue(),clipped
    if animated:
        draw.rounded_rectangle((43, 43, 237, 133), radius=22, fill='#e9fbf5', outline=ink, width=2)
        label = 'Picking...' if phase is not None else ('Your numbers' if kind == 'numbers' else 'Chosen!')
        draw.text((140, 77), label, anchor='mm', font=regular, fill=ink)
        for i in range(5):
            x = 101 + i * 19
            y = 103 + (int(phase * 8 + i) % 2 * 5 if phase is not None else 0)
            draw.ellipse((x-4,y-4,x+4,y+4),fill='#e7a352' if i%2 else '#50b8a1')
    # A short result deserves a prominent answer, while long instructions keep
    # the smaller wrapped reading card and explicit clipping receipt.
    large=ImageFont.load_default(size=28)
    prominent=_lines(draw,text,large,240)
    if phase is None and len(str(text))<=70 and len(prominent)<=2:
        top=145
        draw.rounded_rectangle((8,top,272,232),radius=18,fill='#f6fffc',outline=mint,width=2)
        for index,line in enumerate(prominent):
            y=188 if len(prominent)==1 else 171+index*32
            draw.text((140,y),line,anchor='mm',font=large,fill=ink)
        output=io.BytesIO()
        canvas.save(output,format='PNG')
        return output.getvalue(),False
    draw.rounded_rectangle((8, top, 272, 232), radius=15, fill='#f6fffc', outline=mint, width=2)
    available = (222-top)//22
    lines = _lines(draw, text, regular, 242)
    clipped = len(lines) > available
    lines = lines[:available]
    if clipped and lines:
        while draw.textlength(lines[-1] + '...', font=regular) > 242:
            lines[-1] = lines[-1][:-1]
        lines[-1] += '...'
    for index,line in enumerate(lines):
        draw.text((19, top + 9 + 22*index),line,font=regular,fill=ink)
    output=io.BytesIO()
    canvas.save(output,format='PNG')
    return output.getvalue(), clipped
