import unittest
from scripts.ibb_google_name_by_name import decision_for, name_similarity, normalize

class ArabicMatchingTests(unittest.TestCase):
    def place(self, name, lat=13.975, lon=44.183, address='Ibb, Yemen'):
        return {'displayName': {'text': name}, 'location': {'latitude': lat, 'longitude': lon}, 'formattedAddress': address}

    def test_arabic_alif_and_diacritics_normalize(self):
        self.assertEqual(normalize('الإلكترونيّات'), normalize('الالكترونيات'))

    def test_exact_name_inside_bbox(self):
        score, decision = decision_for('مركز أحمد حاجب للإلكترونيات', self.place('مركز أحمد حاجب للإلكترونيات'))
        self.assertEqual(score, 1.0)
        self.assertEqual(decision, 'exact_name_in_ibb_bbox')

    def test_exact_name_outside_bbox_needs_review(self):
        _, decision = decision_for('صيدلية الزهراء', self.place('صيدلية الزهراء', lat=15.3, lon=44.2, address='Sanaa, Yemen'))
        self.assertEqual(decision, 'name_match_outside_ibb_bbox_review')

    def test_missing_coordinates_never_auto_confirms(self):
        place = {'displayName': {'text':'مخبز الأمل'}, 'formattedAddress':'Ibb, Yemen'}
        _, decision = decision_for('مخبز الأمل', place)
        self.assertEqual(decision, 'exact_name_address_only_review_required')

    def test_ambiguous_branch_name_is_not_high_confidence(self):
        score = name_similarity('صيدلية الزهراء 1 (فرع مستشفى الثورة)', 'صيدلية الزهراء')
        self.assertLess(score, 0.74)

    def test_fuzzy_name_stays_review_candidate(self):
        _, decision = decision_for('الخطابي للجوالات والإلكترونيات', self.place('معرض الخطابي للجوالات والالكترونيات'))
        self.assertEqual(decision, 'strong_candidate_review_required')

if __name__ == '__main__':
    unittest.main()
