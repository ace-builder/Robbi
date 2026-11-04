# Text toolbox

Count words and characters or change text case and whitespace offline.

Use the generated forms in Robbi V2 App library, or ask Robbi using the example phrases. Its implementation travels inside this project; it requires only the declared Robbi SDK and no provider keys or network connection.

Up to 4000 input characters. Word counts split on whitespace, so they are not linguistic segmentation for all languages. Character counts measure Unicode code points, not visual grapheme clusters. Empty text has zero lines. Case conversion is Unicode-aware but not locale-specific.
